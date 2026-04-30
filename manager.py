import os
import glob
import cv2
import yaml
import subprocess
import shutil
import sys
import numpy as np
import time
import random 
import json
import threading
import logging
import requests
import base64
import re
import pyexiv2
import xml.sax.saxutils as saxutils
from datetime import datetime
from werkzeug.utils import secure_filename
from flask import Flask, render_template_string, request, jsonify, send_file
from ultralytics import YOLO 

# --- Setup & Configuration ---
app = Flask(__name__)
MEDIA_DIR = "media"
CONFIG_FILE = "app_config.json"
os.makedirs(MEDIA_DIR, exist_ok=True)
os.makedirs("logs", exist_ok=True)

log_formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

error_handler = logging.FileHandler('logs/error.log')
error_handler.setLevel(logging.ERROR)
error_handler.setFormatter(log_formatter)

training_logger = logging.getLogger('training')
training_logger.setLevel(logging.INFO)
training_handler = logging.FileHandler('logs/training.log')
training_handler.setFormatter(log_formatter)
training_logger.addHandler(training_handler)
training_logger.addHandler(error_handler)

access_logger = logging.getLogger('access')
access_logger.setLevel(logging.INFO)
access_logger.addHandler(logging.StreamHandler())

# --- Application State ---
state = {
    "classes": ["object"],
    "available_models": [],
    "status_text": "Ready.",
    "remote_ip": "",
    
    # OpenAI Compatible Vision LLM Config
    "oai_endpoint": "https://api.openai.com/v1/chat/completions",
    "oai_key": "",
    "oai_model": "gpt-4o-mini",
    "oai_prompt": "Describe this image in a brief, highly detailed paragraph suitable for photo metadata."
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                config_data = json.load(f)
            for k, v in config_data.items():
                if k in state: state[k] = v
        except Exception as e:
            access_logger.error(f"Failed to load config: {e}")

def save_config():
    keys_to_save = ["remote_ip", "oai_endpoint", "oai_key", "oai_model", "oai_prompt"]
    try:
        with open(CONFIG_FILE, "w") as f:
            json.dump({k: state.get(k) for k in keys_to_save}, f, indent=4)
    except Exception as e:
        access_logger.error(f"Failed to save config: {e}")

def load_classes():
    class_file = os.path.join(MEDIA_DIR, "classes.txt")
    if os.path.exists(class_file):
        with open(class_file, "r") as f:
            loaded = [line.strip() for line in f.readlines() if line.strip()]
            if loaded: state["classes"] = loaded

def save_classes():
    class_file = os.path.join(MEDIA_DIR, "classes.txt")
    with open(class_file, "w") as f:
        for c in state["classes"]: f.write(f"{c}\n")

def populate_model_selector():
    search_path = os.path.join(MEDIA_DIR, "runs/detect/train*/weights/best.pt")
    models = glob.glob(search_path)
    models.sort(key=os.path.getmtime)
    state["available_models"] = models

load_config()
load_classes()
populate_model_selector()

# --- pyexiv2 & YOLO Sync Subsystems ---
def sync_yolo_labels(filename, regions):
    """Generates the lightweight YOLO .txt sidecar used ONLY by the training worker"""
    for reg in regions:
        if reg['class_name'] not in state["classes"]:
            state["classes"].append(reg['class_name'])
    save_classes()
    
    txt_path = os.path.join(MEDIA_DIR, os.path.splitext(filename)[0] + ".txt")
    if not regions:
        if os.path.exists(txt_path): os.remove(txt_path)
        return
        
    with open(txt_path, "w") as f:
        for reg in regions:
            cls_id = state["classes"].index(reg['class_name'])
            f.write(f"{cls_id} {reg['cx']:.6f} {reg['cy']:.6f} {reg['w']:.6f} {reg['h']:.6f}\n")

def read_metadata(filepath):
    """Strictly loads metadata natively from the image using pyexiv2 (XMP/IPTC Standards)"""
    try:
        tags = []
        desc = ""
        regions = []
        
        # Prioritize reading from a .xmp sidecar if it exists, fallback to native file
        xmp_path = os.path.splitext(filepath)[0] + '.xmp'
        target_file = xmp_path if os.path.exists(xmp_path) else filepath

        with pyexiv2.Image(target_file) as img:
            xmp = img.read_xmp()
            
            # Read Booru Tags
            val = xmp.get('Xmp.dc.subject', [])
            if isinstance(val, list): tags = val
            elif isinstance(val, str): tags = [val]
                
            # Read Description
            val = xmp.get('Xmp.dc.description', "")
            if isinstance(val, dict): desc = val.get('x-default', '')
            elif isinstance(val, str): desc = val
                
            # Parse XMP IPTC ImageRegion structs
            region_keys = [k for k in xmp.keys() if 'ImageRegion[' in k]
            indices = set()
            for k in region_keys:
                m = re.search(r'\[(\d+)\]', k)
                if m: indices.add(m.group(1))
                    
            for idx in indices:
                prefix = f'Xmp.iptcExt.ImageRegion[{idx}]'
                try:
                    name = xmp.get(f'{prefix}/iptcExt:RegionName', 'object')
                    w = float(xmp.get(f'{prefix}/iptcExt:RegionBoundary/iptcExt:rbW', 0))
                    h = float(xmp.get(f'{prefix}/iptcExt:RegionBoundary/iptcExt:rbH', 0))
                    left = float(xmp.get(f'{prefix}/iptcExt:RegionBoundary/iptcExt:rbX', 0))
                    top = float(xmp.get(f'{prefix}/iptcExt:RegionBoundary/iptcExt:rbY', 0))
                    
                    if w > 0 and h > 0:
                        cx = left + (w / 2)
                        cy = top + (h / 2)
                        regions.append({"class_name": name, "cx": cx, "cy": cy, "w": w, "h": h})
                except Exception:
                    pass
                    
        return {"tags": tags, "description": desc, "regions": regions}
    except Exception as e:
        access_logger.error(f"Error reading metadata with pyexiv2: {e}")
        return {"tags": [], "description": "", "regions": []}

def write_metadata(filepath, tags, description, regions):
    try:
        filename = os.path.basename(filepath)
        sync_yolo_labels(filename, regions) # Sync parallel lightweight sidecar strictly for the YOLO trainer

        xmp_path = os.path.splitext(filepath)[0] + '.xmp'
        
        # Since Exiv2 cannot natively write to BMFF/JXL formats without crashing, 
        # and modifying complex struct bags via pyexiv2 dictionaries triggers Toolkit Error 102,
        # we bypass the library bugs by generating a beautifully clean, industry-standard XMP Sidecar manually.
        
        esc = saxutils.escape
        
        subject_xml = ""
        if tags:
            subject_xml = "<dc:subject>\n    <rdf:Bag>\n" + "".join([f"     <rdf:li>{esc(tag)}</rdf:li>\n" for tag in tags]) + "    </rdf:Bag>\n   </dc:subject>"
            
        desc_xml = ""
        if description:
            desc_xml = f'<dc:description>\n    <rdf:Alt>\n     <rdf:li xml:lang="x-default">{esc(description)}</rdf:li>\n    </rdf:Alt>\n   </dc:description>'
            
        regions_xml = ""
        if regions:
            regions_xml = "<iptcExt:ImageRegion>\n    <rdf:Bag>\n"
            for box in regions:
                rx = box['cx'] - (box['w'] / 2)
                ry = box['cy'] - (box['h'] / 2)
                regions_xml += f"""     <rdf:li rdf:parseType="Resource">
      <iptcExt:RegionName>{esc(box['class_name'])}</iptcExt:RegionName>
      <iptcExt:RegionBoundary rdf:parseType="Resource">
       <iptcExt:rbShape>rectangle</iptcExt:rbShape>
       <iptcExt:rbUnit>relative</iptcExt:rbUnit>
       <iptcExt:rbX>{rx:.6f}</iptcExt:rbX>
       <iptcExt:rbY>{ry:.6f}</iptcExt:rbY>
       <iptcExt:rbW>{box['w']:.6f}</iptcExt:rbW>
       <iptcExt:rbH>{box['h']:.6f}</iptcExt:rbH>
      </iptcExt:RegionBoundary>
     </rdf:li>\n"""
            regions_xml += "    </rdf:Bag>\n   </iptcExt:ImageRegion>"

        xmp_content = f"""<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="PythonSidecar">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about="" 
    xmlns:dc="http://purl.org/dc/elements/1.1/" 
    xmlns:iptcExt="http://iptc.org/std/Iptc4xmpExt/2008-02-29/">
   {subject_xml}
   {desc_xml}
   {regions_xml}
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>"""
        
        # Directly write the validated XML structure to the sidecar
        with open(xmp_path, 'w', encoding='utf-8') as f:
            f.write(xmp_content)

        return True
    except Exception as e:
        access_logger.error(f"Error writing metadata: {e}")
        return False

def get_base64_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

# --- YOLO Training Workers ---
def remote_yolo_train_worker(abs_folder, dataset_dir, config, remote_ip):
    try:
        state["status_text"] = f"Zipping dataset and offloading to {remote_ip}..."
        zip_path = os.path.join(abs_folder, "yolo_dataset.zip")
        shutil.make_archive(zip_path.replace('.zip', ''), 'zip', dataset_dir)
        
        url_start = f"http://{remote_ip}/api/start_train"
        with open(zip_path, 'rb') as f:
            res = requests.post(url_start, files={'dataset': f}, data={'config': json.dumps(config)}, timeout=30)
            
        if res.status_code != 200: raise Exception(f"Failed to start remote job. {res.text}")
            
        job_id = res.json()['job_id']
        state["status_text"] = f"Remote training active! Job ID: {job_id}"
        
        url_status = f"http://{remote_ip}/api/status/{job_id}"
        while True:
            time.sleep(3)
            status_res = requests.get(url_status, timeout=10).json()
            status = status_res.get('status', 'failed')
            remote_log = status_res.get('log', '')

            if remote_log:
                with open("logs/training.log", "w") as log_file:
                    log_file.write(f"--- LIVE REMOTE LOGSTREAM ({remote_ip}) ---\n" + remote_log)
                    
            if status in ['completed', 'failed']: break

        if status == 'completed':
            state["status_text"] = "Remote training complete! Downloading weights..."
            dl_res = requests.get(f"http://{remote_ip}/api/download/{job_id}", timeout=60)
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target_dir = os.path.join(abs_folder, f"runs/detect/train_remote_{timestamp}/weights")
            os.makedirs(target_dir, exist_ok=True)
            with open(os.path.join(target_dir, "best.pt"), 'wb') as f: f.write(dl_res.content)
                
            populate_model_selector()
            state["status_text"] = "Download complete! Ready for Auto-Tagging."
        else:
            raise Exception("Remote Job Status reported failure. Check logs.")
            
    except Exception as e:
        state["status_text"] = f"Remote Training error: {e}"
    finally:
        if os.path.exists(zip_path): os.remove(zip_path)

def yolo_train_worker(abs_folder, dataset_dir, yaml_path, epochs, batch, imgsz, device, base_model):
    try:
        training_logger.info(f"--- Starting LOCAL YOLO Training ---")
        patch_script = """
import sys
from ultralytics import YOLO
yaml_path, base_model, epochs, batch, imgsz, device = sys.argv[1:7]
epochs, batch, imgsz = int(epochs), int(batch), int(imgsz)
if device == "-1": device = -1
elif device.isdigit(): device = int(device)

model = YOLO(base_model)
model.train(data=yaml_path, epochs=epochs, batch=batch, imgsz=imgsz, device=device)
"""
        cmd = [sys.executable, "-c", patch_script, yaml_path, base_model, str(epochs), str(batch), str(imgsz), str(device)]
        
        with open("logs/training.log", "w") as log_file:
            log_file.write(f"[{datetime.now()}] --- YOLO Training Started ---\n")
            log_file.flush()
            subprocess.run(cmd, check=True, cwd=abs_folder, stdout=log_file, stderr=subprocess.STDOUT)
            
        populate_model_selector()
        state["status_text"] = "Training Complete! Ready for Auto-Tagging."
    except Exception as e:
        state["status_text"] = f"Training error: {e}"
        training_logger.error(f"Training worker error: {e}")


# --- Web Routes ---
@app.route("/")
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route("/training_portal")
def training_portal():
    return render_template_string(TRAINING_PORTAL_TEMPLATE)

@app.route("/api/state", methods=["GET"])
def get_state():
    return jsonify({
        "classes": state["classes"],
        "models": state["available_models"],
        "status_text": state["status_text"],
        "remote_ip": state["remote_ip"],
        "oai_endpoint": state["oai_endpoint"],
        "oai_key": state["oai_key"],
        "oai_model": state["oai_model"],
        "oai_prompt": state["oai_prompt"]
    })

@app.route("/api/update_settings", methods=["POST"])
def update_settings():
    data = request.json
    state["oai_endpoint"] = data.get("oai_endpoint", state["oai_endpoint"])
    state["oai_key"] = data.get("oai_key", state["oai_key"])
    state["oai_model"] = data.get("oai_model", state["oai_model"])
    state["oai_prompt"] = data.get("oai_prompt", state["oai_prompt"])
    save_config()
    return jsonify({"success": True})

@app.route("/api/upload", methods=["POST"])
def api_upload():
    if 'file' not in request.files: return jsonify({"success": False})
    file = request.files['file']
    filename = secure_filename(file.filename)
    base_name, _ = os.path.splitext(filename)
    jxl_name = f"{base_name}.jxl"
    
    temp_path = os.path.join(MEDIA_DIR, filename)
    jxl_path = os.path.join(MEDIA_DIR, jxl_name)
    file.save(temp_path)
    
    try:
        subprocess.run(['cjxl', temp_path, jxl_path, '-d', '0'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if temp_path != jxl_path: os.remove(temp_path)
        return jsonify({"success": True, "filename": jxl_name})
    except Exception as e:
        return jsonify({"success": True, "filename": filename, "warning": "Original format retained."})

@app.route("/api/list", methods=["GET"])
def api_list():
    files = [f for f in os.listdir(MEDIA_DIR) if os.path.isfile(os.path.join(MEDIA_DIR, f)) and not f.startswith('.') and not f.endswith('.txt') and not f.endswith('.xmp')]
    return jsonify({"success": True, "files": sorted(files)})

@app.route("/api/file/<filename>")
def api_file(filename):
    filepath = os.path.join(MEDIA_DIR, secure_filename(filename))
    if os.path.exists(filepath):
        mime = 'image/jxl' if filename.lower().endswith('.jxl') else None
        return send_file(filepath, mimetype=mime)
    return "", 404

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    data = request.json
    filename = secure_filename(data.get("filename", ""))
    filepath = os.path.join(MEDIA_DIR, filename)
    if not os.path.exists(filepath): return jsonify({"success": False})
    
    if data.get("action") == "read":
        return jsonify({"success": True, "metadata": read_metadata(filepath)})
    elif data.get("action") == "write":
        return jsonify({"success": write_metadata(filepath, data.get("tags", []), data.get("description", ""), data.get("regions", []))})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    filename = secure_filename(request.json.get("filename", ""))
    base_name = os.path.splitext(filename)[0]
    
    # Wipe the primary file, sidecar, and YOLO labels cleanly
    for ext in ['.jxl', '.txt', '.xmp', os.path.splitext(filename)[1]]:
        path = os.path.join(MEDIA_DIR, base_name + ext)
        if os.path.exists(path): 
            os.remove(path)
            
    return jsonify({"success": True})

@app.route("/api/auto_tag", methods=["POST"])
def auto_tag():
    model_path = request.json.get("model")
    filename = secure_filename(request.json.get("filename", ""))
    jxl_path = os.path.join(MEDIA_DIR, filename)
    
    if not os.path.exists(model_path) or not os.path.exists(jxl_path):
        return jsonify({"success": False, "error": "Invalid model or file."})
        
    temp_jpg = os.path.join(MEDIA_DIR, f"temp_{int(time.time())}.jpg")
    try:
        # Decode JXL to temporary JPG for YOLO inference
        subprocess.run(['djxl', jxl_path, temp_jpg], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        model = YOLO(model_path)
        results = model(temp_jpg, verbose=False, conf=0.25)
        
        new_regions = []
        if results[0].boxes is not None:
            for box in results[0].boxes:
                cls_id = int(box.cls[0].item())
                cls_name = results[0].names[cls_id]
                cx, cy, w, h = box.xywhn[0].tolist()
                new_regions.append({"class_name": cls_name, "cx": cx, "cy": cy, "w": w, "h": h})
                
                if cls_name not in state["classes"]:
                    state["classes"].append(cls_name)
        save_classes()
        os.remove(temp_jpg)
        return jsonify({"success": True, "regions": new_regions})
    except Exception as e:
        if os.path.exists(temp_jpg): os.remove(temp_jpg)
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/auto_describe", methods=["POST"])
def auto_describe():
    filename = secure_filename(request.json.get("filename", ""))
    jxl_path = os.path.join(MEDIA_DIR, filename)
    
    if not os.path.exists(jxl_path):
        return jsonify({"success": False, "error": "File not found."})

    endpoint = state.get("oai_endpoint", "").strip()
    model = state.get("oai_model", "").strip()
    api_key = state.get("oai_key", "").strip()
    prompt = state.get("oai_prompt", "").strip()

    if not endpoint or not model:
        return jsonify({"success": False, "error": "LLM Endpoint or Model not configured. Check AI Settings."})

    temp_jpg = os.path.join(MEDIA_DIR, f"temp_desc_{int(time.time())}.jpg")
    try:
        # LLMs don't read JXL well, convert to standard JPEG
        subprocess.run(['djxl', jxl_path, temp_jpg], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        base64_image = get_base64_image(temp_jpg)

        headers = {"Content-Type": "application/json"}
        if api_key: headers["Authorization"] = f"Bearer {api_key}"

        payload = {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{base64_image}"}}
                    ]
                }
            ],
            "max_tokens": 500
        }

        response = requests.post(endpoint, headers=headers, json=payload, timeout=45)
        response.raise_for_status()
        desc = response.json()['choices'][0]['message']['content']

        os.remove(temp_jpg)
        return jsonify({"success": True, "description": desc})
    except Exception as e:
        if os.path.exists(temp_jpg): os.remove(temp_jpg)
        access_logger.error(f"OAI Auto-Describe Error: {e}")
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/train", methods=["POST"])
def train():
    data = request.json or {}
    epochs = data.get("epochs", 100)
    batch = data.get("batch", 4)
    imgsz = data.get("imgsz", 640)
    device = data.get("device", "-1")
    base_model = data.get("base_model", "yolo11n.pt")
    remote_ip = data.get("remote_ip", "").strip()
    
    state["status_text"] = "Decoding JXL dataset & converting for YOLO..."
    
    abs_folder = os.path.abspath(MEDIA_DIR)
    dataset_dir = os.path.join(abs_folder, "yolo_dataset")
    shutil.rmtree(dataset_dir, ignore_errors=True)
    
    img_tr, lab_tr = os.path.join(dataset_dir, "images", "train"), os.path.join(dataset_dir, "labels", "train")
    img_val, lab_val = os.path.join(dataset_dir, "images", "val"), os.path.join(dataset_dir, "labels", "val")
    for d in [img_tr, lab_tr, img_val, lab_val]: os.makedirs(d, exist_ok=True)
    
    # Find active labels and matching images
    labeled_bases = [os.path.splitext(f)[0] for f in os.listdir(MEDIA_DIR) if f.endswith('.txt') and f != 'classes.txt' and os.path.getsize(os.path.join(MEDIA_DIR, f)) > 0]
    valid_pairs = [b for b in labeled_bases if os.path.exists(os.path.join(MEDIA_DIR, b + ".jxl"))]

    if not valid_pairs:
        state["status_text"] = "No region tags found! Label some images first."
        return jsonify({"success": False})

    random.shuffle(valid_pairs)
    val_count = max(1, int(len(valid_pairs) * 0.05)) if len(valid_pairs) > 1 else 0
    val_bases, train_bases = valid_pairs[:val_count], valid_pairs[val_count:]

    # JXL Decoding Loop for YOLO
    for b in train_bases:
        subprocess.run(['djxl', os.path.join(MEDIA_DIR, b + ".jxl"), os.path.join(img_tr, b + ".jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.copy(os.path.join(MEDIA_DIR, b + ".txt"), os.path.join(lab_tr, b + ".txt"))
    for b in val_bases:
        subprocess.run(['djxl', os.path.join(MEDIA_DIR, b + ".jxl"), os.path.join(img_val, b + ".jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.copy(os.path.join(MEDIA_DIR, b + ".txt"), os.path.join(lab_val, b + ".txt"))

    yaml_path = os.path.join(dataset_dir, "dataset.yaml")
    with open(yaml_path, "w") as f:
        yaml.dump({"path": dataset_dir, "train": "images/train", "val": "images/val", "nc": len(state["classes"]), "names": state["classes"]}, f)

    if remote_ip:
        state["remote_ip"] = remote_ip
        save_config()
        threading.Thread(target=remote_yolo_train_worker, args=(abs_folder, dataset_dir, data, remote_ip)).start()
    else:
        state["status_text"] = f"Starting LOCAL YOLO Training... ({len(train_bases)} Train | {len(val_bases)} Val)"
        threading.Thread(target=yolo_train_worker, args=(abs_folder, dataset_dir, yaml_path, epochs, batch, imgsz, device, base_model)).start()
        
    return jsonify({"success": True})

@app.route("/api/training_log", methods=["GET"])
def get_training_log():
    if not os.path.exists('logs/training.log'): return jsonify({"log": "Awaiting process start..."})
    with open('logs/training.log', 'r') as f: return jsonify({"log": "".join(f.readlines()[-200:])})

@app.route("/tailwind", methods=["GET"])
def get_tailwind():
    if not os.path.exists('tailwindcss.js'):
        return jsonify({"error": "tailwindcss.js not found"}), 404
    with open('tailwindcss.js', 'r') as f:
        content = f.read()
    return content, 200, {'Content-Type': 'application/javascript; charset=utf-8'}


# --- Frontend Templates ---
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>AI Media & Asset Manager</title>
    <script src="/tailwind"></script>
    <style>
        .dropzone-active { background-color: #374151; border-color: #60A5FA; }
        .gallery-item { 
            cursor: pointer; 
            transition: transform 0.1s; 
            aspect-ratio: 1 / 1; /* Guaranteed square grid item to prevent overlapping */
            position: relative;
        }
        .gallery-item:hover { transform: scale(1.02); border-color: #60A5FA; z-index: 10; }
        .selected-item { border-color: #3B82F6; box-shadow: 0 0 10px #3B82F6; }
        canvas { cursor: crosshair; display: block; }
        
        .resizable-pane {
            resize: horizontal;
            overflow: hidden;
            position: relative;
        }
        .resizable-pane::after {
            content: '||';
            position: absolute;
            right: 2px;
            bottom: 2px;
            font-size: 14px;
            color: #6B7280;
            pointer-events: none;
            letter-spacing: -2px;
        }
    </style>
</head>
<body class="bg-gray-900 text-white font-sans h-screen flex w-full overflow-hidden">

    <!-- Left Pane: Library & Dropzone -->
    <div class="flex flex-col border-r border-gray-700 h-full resizable-pane bg-gray-900 z-10" style="width: 60%; min-width: 350px; max-width: 80vw; padding-bottom: 20px;">
        <div class="p-4 bg-gray-800 border-b border-gray-700 flex justify-between items-center shadow-sm z-10 flex-shrink-0">
            <h1 class="text-2xl font-bold text-blue-400">Media Library</h1>
            <span class="text-sm text-gray-400" id="file_count">0 Items</span>
        </div>

        <div id="dropzone" class="m-4 border-2 border-dashed border-gray-600 rounded-lg p-6 text-center text-gray-400 transition-colors flex flex-col items-center justify-center bg-gray-800 flex-shrink-0">
            <svg class="w-10 h-10 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"></path></svg>
            <p class="font-bold">Drag & Drop Images Here</p>
            <p class="text-xs text-gray-500 mt-1">Automatically converted and stored as lossless .JXL</p>
            <input type="file" id="file_input" multiple class="hidden">
            <button onclick="document.getElementById('file_input').click()" class="mt-4 bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded text-sm font-semibold transition shadow">Browse Files</button>
        </div>

        <div class="flex-1 overflow-y-auto p-4 content-start">
            <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4" id="gallery_grid"></div>
        </div>
    </div>

    <!-- Right Pane: Editor & YOLO -->
    <div class="flex-1 bg-gray-800 flex flex-col h-full shadow-xl min-w-[320px] overflow-hidden relative">
        <div class="p-4 border-b border-gray-700 flex justify-between items-center flex-shrink-0 z-10 bg-gray-800">
            <h2 class="text-xl font-bold">Metadata Editor</h2>
            <button id="btn_delete" onclick="deleteCurrentFile()" class="hidden text-red-400 hover:text-red-300 transition text-sm flex items-center">Delete File</button>
        </div>

        <div id="editor_panel" class="p-4 flex flex-col opacity-50 pointer-events-none transition-opacity border-b border-gray-700 pb-6 flex-1 overflow-y-auto">
            <p id="selected_filename" class="text-sm font-mono text-blue-400 truncate mb-2 border-b border-gray-700 pb-2 flex-shrink-0">No file selected</p>
            
            <!-- Canvas container takes all possible space and enables vertical scrolling to make tall images massive -->
            <div id="canvas_container" class="bg-black rounded shadow w-full relative border border-gray-700 flex-1 overflow-y-auto overflow-x-hidden min-h-[300px]">
                <canvas id="media_canvas"></canvas>
            </div>
            
            <!-- Region Toggle -->
            <div class="flex justify-between items-center mt-2 flex-shrink-0">
                <span class="text-xs text-gray-400">Click & Drag on image to add a bounding box</span>
                <label class="text-sm text-gray-300 flex items-center gap-2 cursor-pointer font-bold hover:text-white transition">
                    <input type="checkbox" id="toggle_regions" checked onchange="drawCanvas()" class="accent-blue-500 w-4 h-4 cursor-pointer">
                    Show Regions
                </label>
            </div>
            
            <div class="mt-4 flex-shrink-0">
                <label class="block text-sm font-bold text-gray-300 mb-1">Booru Tags (XMP-dc:Subject)</label>
                <input type="text" id="meta_tags" placeholder="e.g. christmas tree, beach" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white focus:border-blue-500">
            </div>

            <div class="mt-4 flex-shrink-0">
                <div class="flex justify-between items-center mb-1">
                    <label class="block text-sm font-bold text-gray-300">Description (XMP-dc:Description)</label>
                    <button onclick="runAutoDescribe()" id="btn_autodescribe" class="text-xs bg-yellow-600 hover:bg-yellow-500 text-white px-2 py-1 rounded shadow transition flex items-center gap-1">
                        ✨ Auto-Describe
                    </button>
                </div>
                <textarea id="meta_desc" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white focus:border-blue-500 resize-y min-h-[80px]" placeholder="Description..."></textarea>
            </div>

            <button onclick="saveMetadata()" id="btn_save" class="w-full bg-green-600 hover:bg-green-500 py-3 rounded font-bold transition shadow text-lg mt-4 flex-shrink-0">Save EXIF Metadata</button>
        </div>

        <!-- YOLO Tools -->
        <div class="p-4 bg-gray-850 flex flex-col space-y-3 flex-shrink-0">
            <div class="flex justify-between items-center border-b border-gray-700 pb-2">
                <h3 class="text-lg font-bold text-purple-400">AI Tooling</h3>
                <div class="flex gap-2">
                    <button onclick="document.getElementById('ai_settings_modal').classList.remove('hidden')" class="text-xs font-normal bg-gray-700 px-2 py-1 rounded hover:bg-gray-600 text-gray-300">⚙️ Settings</button>
                    <a href="/training_portal" target="_blank" class="text-xs font-normal text-purple-300 bg-gray-700 px-2 py-1 rounded hover:bg-gray-600 border border-purple-800">Trainer ↗</a>
                </div>
            </div>
            
            <div id="yolo_controls" class="opacity-50 pointer-events-none transition-opacity">
                <label class="block text-sm text-gray-300 mb-1">Trained Models</label>
                <select id="model_selector" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white mb-2"></select>
                
                <button onclick="runAutoTag()" id="btn_autotag" class="w-full bg-indigo-600 hover:bg-indigo-500 py-2 rounded font-bold transition text-sm">Auto-Tag Current Image</button>
            </div>
            
            <button onclick="quickTrain()" class="w-full bg-purple-600 hover:bg-purple-500 py-2 rounded font-bold transition text-sm mt-2">Quick Train (Dataset Sync)</button>
            
            <div class="mt-auto border-t border-gray-700 pt-2">
                <p class="text-xs text-gray-400 font-bold">AI Status:</p>
                <p id="status_text" class="text-xs text-yellow-400 break-words">Ready.</p>
            </div>
        </div>
    </div>

    <!-- Region Modal -->
    <div id="region_modal" class="hidden absolute inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
        <div class="bg-gray-800 p-6 rounded-lg shadow-xl w-80 border border-gray-600">
            <h2 class="text-lg font-bold mb-4">Name Region</h2>
            <input type="text" id="modal_region_name" placeholder="e.g. Uncle Bob" class="w-full p-2 bg-gray-700 rounded mb-6 border border-gray-600">
            <div class="flex justify-end space-x-3">
                <button onclick="cancelRegion()" class="bg-gray-600 px-4 py-2 rounded">Cancel</button>
                <button onclick="saveRegion()" class="bg-blue-600 px-4 py-2 rounded">Add</button>
            </div>
        </div>
    </div>

    <!-- AI Settings Modal -->
    <div id="ai_settings_modal" class="hidden absolute inset-0 bg-black bg-opacity-70 flex items-center justify-center z-50">
        <div class="bg-gray-800 p-6 rounded-lg shadow-xl w-96 border border-gray-600">
            <h2 class="text-lg font-bold mb-4 text-purple-400">OAI Vision Settings</h2>
            
            <label class="block text-xs text-gray-400 mb-1">Endpoint URL</label>
            <input type="text" id="cfg_endpoint" class="w-full p-2 bg-gray-700 rounded mb-3 border border-gray-600 text-sm">
            
            <label class="block text-xs text-gray-400 mb-1">API Key (Optional for Local)</label>
            <input type="password" id="cfg_apikey" class="w-full p-2 bg-gray-700 rounded mb-3 border border-gray-600 text-sm">
            
            <label class="block text-xs text-gray-400 mb-1">Model Name</label>
            <input type="text" id="cfg_model" class="w-full p-2 bg-gray-700 rounded mb-3 border border-gray-600 text-sm">
            
            <label class="block text-xs text-gray-400 mb-1">System Prompt</label>
            <textarea id="cfg_prompt" class="w-full p-2 bg-gray-700 rounded mb-4 border border-gray-600 text-sm h-20 resize-none"></textarea>

            <div class="flex justify-end space-x-3 border-t border-gray-700 pt-4">
                <button onclick="document.getElementById('ai_settings_modal').classList.add('hidden')" class="bg-gray-600 px-4 py-2 rounded text-sm">Close</button>
                <button onclick="saveAiSettings()" class="bg-green-600 px-4 py-2 rounded text-sm font-bold">Save Settings</button>
            </div>
        </div>
    </div>

    <script>
        const dropzone = document.getElementById('dropzone');
        const fileInput = document.getElementById('file_input');
        let currentFile = null;
        let currentRegions = [];
        
        // Canvas Setup
        const canvas = document.getElementById('media_canvas');
        const ctx = canvas.getContext('2d');
        let currentImgObj = new Image();
        let drawing = false;
        let startX = 0, startY = 0, curX = 0, curY = 0, pendingBox = null;
        let hasLoadedSettings = false;

        async function fetchState() {
            try {
                const res = await fetch('/api/state');
                const state = await res.json();
                document.getElementById('status_text').innerText = state.status_text;
                
                const sel = document.getElementById('model_selector');
                const currentVal = sel.value;
                sel.innerHTML = state.models.length ? '' : '<option value="">No Models Found</option>';
                state.models.forEach(m => {
                    let opt = document.createElement('option');
                    opt.value = m;
                    let parts = m.split(/[\\\\/]/);
                    opt.text = parts.length >= 3 ? parts.slice(-3).join('/') : m;
                    sel.add(opt);
                });
                if(currentVal) sel.value = currentVal;

                if (!hasLoadedSettings) {
                    document.getElementById('cfg_endpoint').value = state.oai_endpoint;
                    document.getElementById('cfg_apikey').value = state.oai_key;
                    document.getElementById('cfg_model').value = state.oai_model;
                    document.getElementById('cfg_prompt').value = state.oai_prompt;
                    hasLoadedSettings = true;
                }
            } catch(e) {}
        }
        setInterval(fetchState, 2000); fetchState();

        async function saveAiSettings() {
            await fetch('/api/update_settings', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    oai_endpoint: document.getElementById('cfg_endpoint').value,
                    oai_key: document.getElementById('cfg_apikey').value,
                    oai_model: document.getElementById('cfg_model').value,
                    oai_prompt: document.getElementById('cfg_prompt').value
                })
            });
            document.getElementById('ai_settings_modal').classList.add('hidden');
        }

        async function loadGallery() {
            const res = await fetch('/api/list');
            const data = await res.json();
            document.getElementById('file_count').innerText = `${data.files.length} Items`;
            const grid = document.getElementById('gallery_grid');
            grid.innerHTML = '';
            
            data.files.forEach(f => {
                const div = document.createElement('div');
                div.className = `gallery-item bg-gray-800 border-2 border-transparent rounded overflow-hidden group`;
                div.id = `thumb_${f}`;
                div.onclick = () => selectFile(f);
                
                const img = document.createElement('img');
                img.src = '/api/file/' + f;
                img.className = "absolute inset-0 w-full h-full object-cover pointer-events-none"; 
                
                const label = document.createElement('div');
                label.className = "absolute bottom-0 w-full bg-black bg-opacity-75 text-xs truncate px-2 py-1 text-center opacity-0 group-hover:opacity-100 pointer-events-none";
                label.innerText = f;
                
                div.appendChild(img); div.appendChild(label); grid.appendChild(div);
            });
            if (currentFile) document.getElementById(`thumb_${currentFile}`)?.classList.add('selected-item');
        }

        async function selectFile(filename) {
            currentFile = filename;
            document.querySelectorAll('.gallery-item').forEach(el => el.classList.remove('selected-item'));
            document.getElementById(`thumb_${filename}`)?.classList.add('selected-item');
            
            document.getElementById('selected_filename').innerText = filename;
            document.getElementById('editor_panel').classList.remove('opacity-50', 'pointer-events-none');
            document.getElementById('yolo_controls').classList.remove('opacity-50', 'pointer-events-none');
            document.getElementById('btn_delete').classList.remove('hidden');
            
            currentImgObj.src = '/api/file/' + filename + '?ts=' + Date.now();
            
            const res = await fetch('/api/metadata', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({action: 'read', filename: filename})
            });
            const data = await res.json();
            if(data.success) {
                document.getElementById('meta_tags').value = data.metadata.tags.join(", ");
                document.getElementById('meta_desc').value = data.metadata.description;
                currentRegions = data.metadata.regions || [];
                drawCanvas();
            }
        }

        async function saveMetadata() {
            if(!currentFile) return;
            const tags = document.getElementById('meta_tags').value.split(',').map(s => s.trim()).filter(s => s);
            const res = await fetch('/api/metadata', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    action: 'write', filename: currentFile, 
                    tags: tags, description: document.getElementById('meta_desc').value, regions: currentRegions
                })
            });
            if((await res.json()).success) alert("EXIF & YOLO Sync Saved!");
        }

        async function deleteCurrentFile() {
            if(!currentFile) return;
            if(confirm(`Delete ${currentFile}?`)) {
                await fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename: currentFile}) });
                currentFile = null; document.getElementById('editor_panel').classList.add('opacity-50', 'pointer-events-none');
                loadGallery();
            }
        }

        async function runAutoTag() {
            if(!currentFile) return;
            const btn = document.getElementById('btn_autotag');
            btn.innerText = "Processing...";
            const res = await fetch('/api/auto_tag', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({filename: currentFile, model: document.getElementById('model_selector').value})
            });
            const data = await res.json();
            if(data.success) {
                currentRegions = currentRegions.concat(data.regions);
                drawCanvas();
            } else alert(data.error);
            btn.innerText = "Auto-Tag Image Regions";
        }

        async function runAutoDescribe() {
            if(!currentFile) return;
            const btn = document.getElementById('btn_autodescribe');
            const originalText = btn.innerHTML;
            btn.innerHTML = `Wait...`;
            btn.disabled = true;

            try {
                const res = await fetch('/api/auto_describe', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({filename: currentFile})
                });
                const data = await res.json();
                if(data.success) {
                    const descBox = document.getElementById('meta_desc');
                    if(descBox.value.trim() !== "") descBox.value += "\\n\\n";
                    descBox.value += data.description;
                } else alert("Error: " + data.error);
            } catch(e) {
                alert("Network error calling LLM Endpoint.");
            }
            
            btn.innerHTML = originalText;
            btn.disabled = false;
        }

        function quickTrain() {
            fetch('/api/train', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({}) });
            alert("Training started in background! Watch the AI Status.");
        }

        // Drag & Drop
        ['dragenter', 'dragover', 'dragleave', 'drop'].forEach(eName => dropzone.addEventListener(eName, e => { e.preventDefault(); e.stopPropagation(); }, false));
        ['dragenter', 'dragover'].forEach(eName => dropzone.addEventListener(eName, () => dropzone.classList.add('dropzone-active'), false));
        ['dragleave', 'drop'].forEach(eName => dropzone.addEventListener(eName, () => dropzone.classList.remove('dropzone-active'), false));
        dropzone.addEventListener('drop', e => handleFiles(e.dataTransfer.files), false);
        fileInput.addEventListener('change', e => handleFiles(e.target.files));

        async function handleFiles(files) {
            const og = dropzone.innerHTML;
            for(let i=0; i<files.length; i++) {
                dropzone.innerHTML = `<p class="text-blue-400 font-bold animate-pulse">Converting ${i+1}/${files.length}...</p>`;
                let fd = new FormData(); fd.append('file', files[i]);
                await fetch('/api/upload', { method: 'POST', body: fd });
            }
            dropzone.innerHTML = og; loadGallery();
        }

        // Canvas Handlers
        currentImgObj.onload = () => drawCanvas();
        window.addEventListener('resize', () => { if(currentFile) drawCanvas(); });

        // Dynamic Resizing Observer for the Canvas element based on drag boundaries
        const canvasContainer = document.getElementById('canvas_container');
        const resizeObserver = new ResizeObserver(() => {
            if(currentFile && currentImgObj.width) drawCanvas();
        });
        if(canvasContainer) resizeObserver.observe(canvasContainer);

        function drawCanvas() {
            if(!currentImgObj.src || !currentImgObj.width) return;
            const aspect = currentImgObj.width / currentImgObj.height;
            const parent = canvas.parentElement;
            
            let drawW = parent.clientWidth;
            let drawH = drawW / aspect;
            
            canvas.width = drawW; 
            canvas.height = drawH;
            
            ctx.clearRect(0,0, canvas.width, canvas.height); 
            ctx.drawImage(currentImgObj, 0, 0, drawW, drawH);
            
            if(document.getElementById('toggle_regions').checked) {
                currentRegions.forEach(box => {
                    const pxX = (box.cx * drawW) - (box.w * drawW / 2), pxY = (box.cy * drawH) - (box.h * drawH / 2);
                    ctx.strokeStyle = '#3B82F6'; ctx.lineWidth = 2; ctx.strokeRect(pxX, pxY, box.w * drawW, box.h * drawH);
                    ctx.fillStyle = '#3B82F6'; ctx.fillRect(pxX, pxY-18, ctx.measureText(box.class_name).width+8, 18);
                    ctx.fillStyle = '#FFF'; ctx.font = "12px sans-serif"; ctx.fillText(box.class_name, pxX+4, pxY-5);
                });
            }
            if(drawing) { ctx.strokeStyle = '#FCD34D'; ctx.strokeRect(startX, startY, curX-startX, curY-startY); }
        }

        canvas.addEventListener('mousedown', e => { if(e.button===0 && currentFile) { startX = e.offsetX; startY = e.offsetY; drawing = true; }});
        canvas.addEventListener('mousemove', e => { if(drawing) { curX = e.offsetX; curY = e.offsetY; drawCanvas(); }});
        canvas.addEventListener('mouseup', e => {
            if(!drawing || e.button!==0) return; drawing = false; curX = e.offsetX; curY = e.offsetY;
            const x1 = Math.min(startX, curX), x2 = Math.max(startX, curX), y1 = Math.min(startY, curY), y2 = Math.max(startY, curY);
            if((x2-x1)<10 || (y2-y1)<10) { drawCanvas(); return; }
            
            // Re-enable visibility if drawing a new box
            if(!document.getElementById('toggle_regions').checked) {
                document.getElementById('toggle_regions').checked = true;
            }

            pendingBox = {cx: ((x1+x2)/2)/canvas.width, cy: ((y1+y2)/2)/canvas.height, w: (x2-x1)/canvas.width, h: (y2-y1)/canvas.height};
            document.getElementById('modal_region_name').value = '';
            document.getElementById('region_modal').classList.remove('hidden');
            setTimeout(() => document.getElementById('modal_region_name').focus(), 100);
        });
        canvas.addEventListener('contextmenu', e => {
            e.preventDefault(); if(!currentFile) return;
            if(!document.getElementById('toggle_regions').checked) return; // Prevent accidental deletion while hidden
            for (let i = currentRegions.length - 1; i >= 0; i--) {
                const b = currentRegions[i], pxW = b.w * canvas.width, pxH = b.h * canvas.height;
                const pxX = (b.cx * canvas.width) - pxW/2, pxY = (b.cy * canvas.height) - pxH/2;
                if (e.offsetX >= pxX && e.offsetX <= pxX+pxW && e.offsetY >= pxY && e.offsetY <= pxY+pxH) {
                    currentRegions.splice(i, 1); drawCanvas(); break; 
                }
            }
        });

        document.getElementById('modal_region_name').addEventListener('keyup', (e) => {
            if(e.key === 'Enter') saveRegion();
            if(e.key === 'Escape') cancelRegion();
        });

        function saveRegion() {
            pendingBox.class_name = document.getElementById('modal_region_name').value.trim() || "region";
            currentRegions.push(pendingBox); pendingBox = null;
            document.getElementById('region_modal').classList.add('hidden'); drawCanvas();
        }
        function cancelRegion() { pendingBox = null; document.getElementById('region_modal').classList.add('hidden'); drawCanvas(); }
        
        loadGallery();
    </script>
</body>
</html>
"""

TRAINING_PORTAL_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <title>Advanced Training Portal</title>
    <script src="/tailwind"></script>
</head>
<body class="bg-gray-900 text-white font-sans h-screen flex flex-col">
    <div class="p-6 bg-gray-800 border-b border-gray-700 flex justify-between items-center">
        <h1 class="text-3xl font-bold text-purple-400">YOLO11 Training Portal</h1>
        <a href="/" class="bg-gray-700 hover:bg-gray-600 px-4 py-2 rounded transition">← Back to Media Manager</a>
    </div>
    <div class="flex flex-1 overflow-hidden">
        <div class="w-1/3 p-6 bg-gray-800 overflow-y-auto border-r border-gray-700 space-y-4">
            <h2 class="text-xl font-bold mb-4 border-b border-gray-700 pb-2">Configuration</h2>
            <div class="bg-indigo-900 p-3 rounded border border-indigo-700">
                <label class="block text-sm font-bold text-indigo-300 mb-1">Remote Worker IP:Port</label>
                <input type="text" id="remote_ip" placeholder="e.g. 192.168.1.50:5000" class="w-full p-2 bg-gray-700 rounded border border-gray-600">
                <p class="text-xs text-indigo-200 mt-1">Leave blank for local training.</p>
            </div>
            <div><label class="block text-sm text-gray-400 mb-1">Base Model</label><select id="base_model" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white"><option value="yolo11n.pt">yolo11n.pt (Nano)</option><option value="yolo11s.pt">yolo11s.pt (Small)</option></select></div>
            <div><label class="block text-sm text-gray-400 mb-1">Epochs</label><input type="number" id="epochs" value="100" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white"></div>
            <div><label class="block text-sm text-gray-400 mb-1">Batch Size</label><input type="number" id="batch" value="4" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white"></div>
            <div><label class="block text-sm text-gray-400 mb-1">Image Size (px)</label><input type="number" id="imgsz" value="640" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white"></div>
            <div><label class="block text-sm text-gray-400 mb-1">Compute Device</label><select id="device" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white"><option value="-1">CPU / Default (-1)</option><option value="0">GPU 0 (0)</option></select></div>
            <button onclick="startTraining()" class="w-full bg-purple-600 hover:bg-purple-700 py-3 mt-4 rounded font-bold transition">Start Training Job</button>
            <div class="mt-8 pt-4 border-t border-gray-700">
                <h3 class="text-yellow-400 font-bold mb-1">App Status:</h3>
                <p id="app_status" class="text-sm text-gray-300">Awaiting input...</p>
            </div>
        </div>
        <div class="flex-1 bg-black p-4 flex flex-col">
            <h2 class="text-gray-400 text-sm mb-2 uppercase tracking-wide">Live Logs (training.log)</h2>
            <pre id="log_output" class="flex-1 bg-gray-900 border border-gray-700 rounded p-4 text-green-400 font-mono text-sm overflow-y-auto whitespace-pre-wrap"></pre>
        </div>
    </div>
    <script>
        let hasLoadedIp = false;
        async function fetchLogs() {
            try {
                const res = await fetch('/api/state');
                const state = await res.json();
                document.getElementById('app_status').innerText = state.status_text;
                if(!hasLoadedIp) { document.getElementById('remote_ip').value = state.remote_ip; hasLoadedIp = true; }
                
                const logEl = document.getElementById('log_output');
                const isBottom = logEl.scrollHeight - logEl.clientHeight <= logEl.scrollTop + 50;
                logEl.innerText = (await (await fetch('/api/training_log')).json()).log;
                if (isBottom) logEl.scrollTop = logEl.scrollHeight;
            } catch(e) {}
        }
        function startTraining() {
            fetch('/api/train', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({
                base_model: document.getElementById('base_model').value, epochs: document.getElementById('epochs').value,
                batch: document.getElementById('batch').value, imgsz: document.getElementById('imgsz').value,
                device: document.getElementById('device').value, remote_ip: document.getElementById('remote_ip').value.trim()
            })});
            alert("Training job sent!");
        }
        setInterval(fetchLogs, 1000); fetchLogs();
    </script>
</body>
</html>
"""

if __name__ == '__main__':
    access_logger.info("Starting Web Application on Port 8000...")
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)