import os
import glob
import cv2
import yaml
import subprocess
import shutil
import sys
import numpy as np
import tempfile
import io
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

metadata_index = {}
thumb_memory_cache = {} # Keeps disk clean, holds downsampled JXL layers in RAM

# --- Helper Functions ---
def get_safe_path(base_dir, user_path):
    """Prevents directory traversal attacks while allowing subfolders"""
    abs_base = os.path.abspath(base_dir)
    # Strip leading slashes from user path to ensure it joins correctly
    clean_path = user_path.lstrip('\\/')
    abs_target = os.path.abspath(os.path.join(base_dir, clean_path))
    if os.path.commonpath([abs_base, abs_target]) == abs_base:
        return abs_target
    return None

def build_metadata_index():
    access_logger.info("Building metadata index...")
    for root, _, filenames in os.walk(MEDIA_DIR):
        # Skip YOLO runs and hidden dirs
        if any(part.startswith('.') or part == 'runs' for part in root.split(os.sep)):
            continue
        for f in filenames:
            if not f.startswith('.') and not f.endswith('.txt') and not f.endswith('.xmp') and not f.endswith('.json'):
                rel_path = os.path.relpath(os.path.join(root, f), MEDIA_DIR).replace('\\', '/')
                if rel_path not in metadata_index:
                    meta = read_metadata(os.path.join(root, f))
                    metadata_index[rel_path] = {
                        "tags": meta.get("tags", []),
                        "description": meta.get("description", "")
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

# --- Deduplication & Hashing Subsystem ---
def get_ahash_for_file(filepath, cache):
    """Calculates a compact 64-bit average hash based on an 8x8 resized gray image."""
    filename = os.path.basename(filepath)
    try:
        mtime = os.path.getmtime(filepath)
        if filename in cache and cache[filename].get('mtime') == mtime:
            return cache[filename]['hash']
            
        temp_jpg = os.path.join(MEDIA_DIR, f"temp_hash_{os.getpid()}_{int(time.time()*1000)}.jpg")
        subprocess.run(['djxl', filepath, temp_jpg], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        
        img = cv2.imread(temp_jpg, cv2.IMREAD_GRAYSCALE)
        if os.path.exists(temp_jpg): os.remove(temp_jpg)
        
        if img is not None:
            resized = cv2.resize(img, (8, 8))
            avg = resized.mean()
            hash_str = ''.join(['1' if b else '0' for b in (resized >= avg).flatten()])
            cache[filename] = {'hash': hash_str, 'mtime': mtime}
            return hash_str
    except Exception as e:
        access_logger.error(f"Error hashing {filename}: {e}")
    return None

# --- pyexiv2 & YOLO Sync Subsystems ---
def sync_yolo_labels(filepath, regions):
    """Generates the lightweight YOLO .txt sidecar used ONLY by the training worker"""
    for reg in regions:
        if reg['class_name'] not in state["classes"]:
            state["classes"].append(reg['class_name'])
    save_classes()
    
    txt_path = os.path.splitext(filepath)[0] + ".txt"
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

        if not os.path.exists(target_file):
            return {"tags": [], "description": "", "regions": []}

        # 1. Read structural tags via pyexiv2 standard read
        with pyexiv2.Image(target_file) as img:
            xmp = img.read_xmp()
            
            # Read Booru Tags
            val = xmp.get('Xmp.dc.subject', [])
            if isinstance(val, list): tags = val
            elif isinstance(val, str): tags = [val]
                
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

        # Bypass pyexiv2 dict bug by extracting description directly from XML if sidecar exists
        if os.path.exists(xmp_path):
            with open(xmp_path, 'r', encoding='utf-8') as f:
                xml_str = f.read()
            # Regex specifically targeting the x-default alt text
            m = re.search(r'<dc:description>\s*<rdf:Alt>\s*<rdf:li[^>]*>(.*?)</rdf:li>', xml_str, re.DOTALL)
            if m:
                extracted = saxutils.unescape(m.group(1).strip())
                if extracted: desc = extracted
                    
        return {"tags": tags, "description": desc, "regions": regions}
    except Exception as e:
        access_logger.error(f"Error reading metadata with pyexiv2: {e}")
        return {"tags": [], "description": "", "regions": []}

def write_metadata(filepath, tags, description, regions):
    try:
        sync_yolo_labels(filepath, regions)

        xmp_path = os.path.splitext(filepath)[0] + '.xmp'
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

        # Update Live Search Index
        rel_path = os.path.relpath(filepath, MEDIA_DIR).replace('\\', '/')
        metadata_index[rel_path] = {"tags": tags, "description": description}

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
    
    # Process Subfolder Target
    folder = request.form.get("folder", "").strip()
    target_dir = get_safe_path(MEDIA_DIR, folder) if folder else MEDIA_DIR
    if not target_dir: return jsonify({"success": False})
    os.makedirs(target_dir, exist_ok=True)
    
    file = request.files['file']
    filename = secure_filename(file.filename)
    base_name, _ = os.path.splitext(filename)
    jxl_name = f"{base_name}.jxl"
    
    temp_path = os.path.join(target_dir, filename)
    jxl_path = os.path.join(target_dir, jxl_name)
    file.save(temp_path)
    
    try:
        subprocess.run(['cjxl', temp_path, jxl_path, '-d', '0'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if temp_path != jxl_path: os.remove(temp_path)
        return jsonify({"success": True, "filename": jxl_name})
    except Exception as e:
        return jsonify({"success": True, "filename": filename, "warning": "Original format retained."})

@app.route("/api/move", methods=["POST"])
def api_move():
    filename = request.json.get("filename", "")
    new_folder = request.json.get("new_folder", "").strip()
    
    old_path = get_safe_path(MEDIA_DIR, filename)
    if not old_path or not os.path.exists(old_path): return jsonify({"success": False})
    
    target_dir = get_safe_path(MEDIA_DIR, new_folder) if new_folder else MEDIA_DIR
    if not target_dir: return jsonify({"success": False})
    os.makedirs(target_dir, exist_ok=True)
    
    base_name = os.path.basename(filename)
    new_path = os.path.join(target_dir, base_name)
    
    if old_path != new_path:
        old_base = os.path.splitext(old_path)[0]
        new_base = os.path.splitext(new_path)[0]
        for ext in ['.jxl', '.txt', '.xmp']:
            if os.path.exists(old_base + ext):
                shutil.move(old_base + ext, new_base + ext)
                
        metadata_index.pop(filename, None)
        build_metadata_index()
        
    return jsonify({"success": True})

@app.route("/api/list", methods=["GET"])
def api_list():
    files_payload = []
    for root, _, filenames in os.walk(MEDIA_DIR):
        if any(part.startswith('.') or part == 'runs' for part in root.split(os.sep)):
            continue
        for f in filenames:
            if not f.startswith('.') and not f.endswith('.txt') and not f.endswith('.xmp') and not f.endswith('.json'):
                rel_path = os.path.relpath(os.path.join(root, f), MEDIA_DIR).replace('\\', '/')
                meta = metadata_index.get(rel_path, {"tags": [], "description": ""})
                files_payload.append({
                    "filename": rel_path,
                    "tags": meta.get("tags", []),
                    "description": meta.get("description", "")
                })
    return jsonify({"success": True, "files": sorted(files_payload, key=lambda x: x['filename'])})

@app.route("/api/file/<path:filename>")
def api_file(filename):
    filepath = get_safe_path(MEDIA_DIR, filename)
    if filepath and os.path.exists(filepath):
        mime = 'image/jxl' if filename.lower().endswith('.jxl') else None
        return send_file(filepath, mimetype=mime)
    return "", 404

@app.route("/api/thumb/<path:filename>")
def api_thumb(filename):
    filepath = get_safe_path(MEDIA_DIR, filename)
    if not filepath or not os.path.exists(filepath): return "", 404

    mtime = os.path.getmtime(filepath)
    if filename in thumb_memory_cache and thumb_memory_cache[filename]['mtime'] == mtime:
        return send_file(io.BytesIO(thumb_memory_cache[filename]['data']), mimetype='image/jpeg')

    temp_jpg = os.path.join(tempfile.gettempdir(), f"preview_{os.getpid()}_{time.time()}.jpg")
    try:
        # djxl progressive decoding downsampling 8 for immense speedup
        subprocess.run(['djxl', filepath, temp_jpg, '--downsampling', '8'], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with open(temp_jpg, 'rb') as f:
            img_data = f.read()
        
        # Max memory capacity of 250 previews
        if len(thumb_memory_cache) > 250:
            thumb_memory_cache.pop(next(iter(thumb_memory_cache)))
            
        thumb_memory_cache[filename] = {'mtime': mtime, 'data': img_data}
        return send_file(io.BytesIO(img_data), mimetype='image/jpeg')
    except Exception:
        return send_file(filepath, mimetype='image/jxl')
    finally:
        if os.path.exists(temp_jpg): os.remove(temp_jpg)

@app.route("/api/metadata", methods=["POST"])
def api_metadata():
    data = request.json
    filename = data.get("filename", "")
    filepath = get_safe_path(MEDIA_DIR, filename)
    
    if not filepath or not os.path.exists(filepath): return jsonify({"success": False})
    
    if data.get("action") == "read":
        return jsonify({"success": True, "metadata": read_metadata(filepath)})
    elif data.get("action") == "write":
        return jsonify({"success": write_metadata(filepath, data.get("tags", []), data.get("description", ""), data.get("regions", []))})

@app.route("/api/delete", methods=["POST"])
def api_delete():
    filename = request.json.get("filename", "")
    filepath = get_safe_path(MEDIA_DIR, filename)
    if filepath:
        base_path = os.path.splitext(filepath)[0]
        # Wipe the primary file, sidecar, and YOLO labels cleanly
        for ext in ['.jxl', '.txt', '.xmp', os.path.splitext(filename)[1]]:
            if os.path.exists(base_path + ext): 
                os.remove(base_path + ext)
        
        thumb_memory_cache.pop(filename, None)
        metadata_index.pop(filename, None)
            
    return jsonify({"success": True})

@app.route("/api/dedup", methods=["POST"])
def dedup():
    files = []
    for root, _, filenames in os.walk(MEDIA_DIR):
        if any(part.startswith('.') or part == 'runs' for part in root.split(os.sep)): continue
        for f in filenames:
            if f.endswith('.jxl'):
                files.append(os.path.relpath(os.path.join(root, f), MEDIA_DIR).replace('\\', '/'))
                
    cache_path = os.path.join(MEDIA_DIR, "ahash_cache.json")
    cache = {}
    
    if os.path.exists(cache_path):
        try:
            with open(cache_path, 'r') as f: cache = json.load(f)
        except Exception: pass
        
    hashes = {}
    for f in files:
        path = get_safe_path(MEDIA_DIR, f)
        h = get_ahash_for_file(path, cache)
        if h: hashes[f] = h
        
    # Write updated hashes to disk cache
    try:
        with open(cache_path, 'w') as f: json.dump(cache, f)
    except: pass
        
    # 1. Compare hashes for similarity candidates
    candidates = []
    files_with_hash = list(hashes.keys())
    for i in range(len(files_with_hash)):
        for j in range(i+1, len(files_with_hash)):
            f1, f2 = files_with_hash[i], files_with_hash[j]
            dist = sum(c1 != c2 for c1, c2 in zip(hashes[f1], hashes[f2]))
            if dist <= 5: # Threshold for identical or lightly transformed images
                candidates.append((f1, f2))
                
    duplicate_pairs = []
    
    # 2. Strict Full Image comparison for verified similarity
    for f1, f2 in candidates:
        temp1 = os.path.join(tempfile.gettempdir(), f"temp_full1_{os.getpid()}_{random.randint(0,999)}.jpg")
        temp2 = os.path.join(tempfile.gettempdir(), f"temp_full2_{os.getpid()}_{random.randint(0,999)}.jpg")
        try:
            subprocess.run(['djxl', get_safe_path(MEDIA_DIR, f1), temp1], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(['djxl', get_safe_path(MEDIA_DIR, f2), temp2], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            img1 = cv2.imread(temp1)
            img2 = cv2.imread(temp2)
            
            if img1 is not None and img2 is not None:
                # Align shapes if they differ
                if img1.shape != img2.shape:
                    img2 = cv2.resize(img2, (img1.shape[1], img1.shape[0]))
                    
                # Mathematical pixel comparison (allow mild JXL compression artifacting diffs)
                diff = cv2.absdiff(img1, img2).mean()
                if diff < 15.0: 
                    duplicate_pairs.append((f1, f2))
        except Exception as e:
            pass
        finally:
            if os.path.exists(temp1): os.remove(temp1)
            if os.path.exists(temp2): os.remove(temp2)
            
    # 3. Group matching pairs together using basic graph components
    adj = {f: [] for f in files}
    for f1, f2 in duplicate_pairs:
        adj[f1].append(f2)
        adj[f2].append(f1)
        
    visited = set()
    groups = []
    for f in files:
        if f not in visited and adj[f]:
            q = [f]
            comp = []
            while q:
                curr = q.pop(0)
                if curr not in visited:
                    visited.add(curr)
                    comp.append(curr)
                    q.extend(adj[curr])
            if len(comp) > 1:
                groups.append(comp)
                
    return jsonify({"success": True, "groups": groups})

@app.route("/api/dedup_merge", methods=["POST"])
def dedup_merge():
    data = request.json
    target = data.get("target", "")
    others = [f for f in data.get("others", []) if f]

    target_path = get_safe_path(MEDIA_DIR, target)
    if not target_path or not os.path.exists(target_path):
        return jsonify({"success": False, "error": "Target not found"})

    try:
        # Read base metadata
        base_meta = read_metadata(target_path)

        for other in others:
            other_path = get_safe_path(MEDIA_DIR, other)
            if not other_path or not os.path.exists(other_path): continue

            other_meta = read_metadata(other_path)

            # Merge Tags (Case-insensitive deduplication)
            merged_tags = []
            seen_tags = set()
            for t in base_meta["tags"] + other_meta["tags"]:
                if t.lower() not in seen_tags:
                    seen_tags.add(t.lower())
                    merged_tags.append(t)
            base_meta["tags"] = merged_tags

            # Merge Description
            d1, d2 = base_meta["description"].strip(), other_meta["description"].strip()
            if d1 and d2 and d1 != d2 and d2 not in d1:
                base_meta["description"] = f"{d1}\n\n{d2}"
            elif d2 and not d1:
                base_meta["description"] = d2

            # Merge Regions (Spatial deduplication)
            for r2 in other_meta["regions"]:
                is_dup = False
                for r1 in base_meta["regions"]:
                    if r1["class_name"] == r2["class_name"]:
                        # If boxes are extremely close, consider them duplicate entries for the same object
                        if abs(r1["cx"] - r2["cx"]) < 0.05 and abs(r1["cy"] - r2["cy"]) < 0.05 and \
                           abs(r1["w"] - r2["w"]) < 0.05 and abs(r1["h"] - r2["h"]) < 0.05:
                            is_dup = True
                            break
                if not is_dup:
                    base_meta["regions"].append(r2)

        # Save merged metadata back to target image
        success = write_metadata(target_path, base_meta["tags"], base_meta["description"], base_meta["regions"])

        if success:
            # Safely delete the redundant copies
            for other in others:
                other_path = get_safe_path(MEDIA_DIR, other)
                base_name = os.path.splitext(other_path)[0]
                for ext in ['.jxl', '.txt', '.xmp', os.path.splitext(other)[1]]:
                    path = base_name + ext
                    if os.path.exists(path):
                        os.remove(path)
                thumb_memory_cache.pop(other, None)
                metadata_index.pop(other, None)
            return jsonify({"success": True})
        else:
            return jsonify({"success": False, "error": "Failed to write merged metadata"})
    except Exception as e:
        return jsonify({"success": False, "error": str(e)})

@app.route("/api/auto_tag", methods=["POST"])
def auto_tag():
    model_path = request.json.get("model")
    filename = request.json.get("filename", "")
    jxl_path = get_safe_path(MEDIA_DIR, filename)
    
    if not os.path.exists(model_path) or not jxl_path or not os.path.exists(jxl_path):
        return jsonify({"success": False, "error": "Invalid model or file."})
        
    temp_jpg = os.path.join(tempfile.gettempdir(), f"temp_{int(time.time())}.jpg")
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
    filename = request.json.get("filename", "")
    jxl_path = get_safe_path(MEDIA_DIR, filename)
    
    if not jxl_path or not os.path.exists(jxl_path):
        return jsonify({"success": False, "error": "File not found."})

    endpoint = state.get("oai_endpoint", "").strip()
    model = state.get("oai_model", "").strip()
    api_key = state.get("oai_key", "").strip()
    prompt = state.get("oai_prompt", "").strip()

    if not endpoint or not model:
        return jsonify({"success": False, "error": "LLM Endpoint or Model not configured. Check AI Settings."})

    temp_jpg = os.path.join(tempfile.gettempdir(), f"temp_desc_{int(time.time())}.jpg")
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
    
    labeled_bases = []
    for root, _, filenames in os.walk(MEDIA_DIR):
        if any(part.startswith('.') or part == 'runs' for part in root.split(os.sep)): continue
        for f in filenames:
            if f.endswith('.txt') and f != 'classes.txt':
                txt_path = os.path.join(root, f)
                if os.path.getsize(txt_path) > 0:
                    labeled_bases.append(os.path.splitext(txt_path)[0])
                    
    valid_pairs = [b for b in labeled_bases if os.path.exists(b + ".jxl")]

    if not valid_pairs:
        state["status_text"] = "No region tags found! Label some images first."
        return jsonify({"success": False})

    random.shuffle(valid_pairs)
    val_count = max(1, int(len(valid_pairs) * 0.05)) if len(valid_pairs) > 1 else 0
    val_bases, train_bases = valid_pairs[:val_count], valid_pairs[val_count:]

    # JXL Decoding Loop for YOLO
    for b in train_bases:
        base_name = os.path.basename(b)
        subprocess.run(['djxl', b + ".jxl", os.path.join(img_tr, base_name + ".jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.copy(b + ".txt", os.path.join(lab_tr, base_name + ".txt"))
    for b in val_bases:
        base_name = os.path.basename(b)
        subprocess.run(['djxl', b + ".jxl", os.path.join(img_val, base_name + ".jpg")], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        shutil.copy(b + ".txt", os.path.join(lab_val, base_name + ".txt"))

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
    if not os.path.exists('static/tailwindcss.js'):
        return jsonify({"error": "static/tailwindcss.js not found"}), 404
    with open('static/tailwindcss.js', 'r') as f:
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

        .resizable-vertical {
            resize: vertical;
            overflow: hidden;
            position: relative;
        }
        .resizable-vertical::after {
            content: '=';
            position: absolute;
            right: 4px;
            bottom: 0px;
            font-size: 18px;
            color: #9CA3AF;
            pointer-events: none;
            line-height: 1;
        }
        
        /* Custom scrollbar for dedup groups */
        .scroller::-webkit-scrollbar { height: 8px; }
        .scroller::-webkit-scrollbar-track { background: #374151; border-radius: 4px; }
        .scroller::-webkit-scrollbar-thumb { background: #4B5563; border-radius: 4px; }
        .scroller::-webkit-scrollbar-thumb:hover { background: #6B7280; }
    </style>
</head>
<body class="bg-gray-900 text-white font-sans h-screen flex w-full overflow-hidden">

    <!-- Left Pane: Library & Dropzone -->
    <div class="flex flex-col border-r border-gray-700 h-full resizable-pane bg-gray-900 z-10" style="width: 60%; min-width: 350px; max-width: 80vw; padding-bottom: 20px;">
        <div class="p-4 bg-gray-800 border-b border-gray-700 flex justify-between items-center shadow-sm z-10 flex-shrink-0">
            <h1 class="text-2xl font-bold text-blue-400">Media Library</h1>
            <div class="flex gap-4 items-center">
                <button id="btn_dedup" onclick="runDedup()" class="text-xs bg-indigo-600 hover:bg-indigo-500 font-bold px-3 py-1.5 rounded transition shadow flex items-center gap-2">
                    🔍 Find Duplicates
                </button>
                <span class="text-sm text-gray-400" id="file_count">0 Items</span>
            </div>
        </div>

        <!-- Search and Filter Bar -->
        <div class="px-4 py-3 bg-gray-800 border-b border-gray-700">
            <input type="text" id="search_input" oninput="filterGallery()" placeholder="Search by tags, filename, folder, or description..." class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-sm text-white focus:border-blue-500 shadow-inner">
        </div>

        <div id="dropzone" class="m-4 border-2 border-dashed border-gray-600 rounded-lg p-6 text-center text-gray-400 transition-colors flex flex-col items-center justify-center bg-gray-800 flex-shrink-0">
            <svg class="w-10 h-10 mb-2" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M7 16a4 4 0 01-.88-7.903A5 5 0 1115.9 6L16 6a5 5 0 011 9.9M15 13l-3-3m0 0l-3 3m3-3v12"></path></svg>
            <p class="font-bold">Drag & Drop Images Here</p>
            <p class="text-xs text-gray-500 mt-1">Automatically converted and stored as lossless .JXL</p>
            
            <div class="mt-4 flex flex-col items-center gap-2">
                <div class="flex items-center gap-2">
                    <span class="text-xs text-gray-500">Target Folder:</span>
                    <input type="text" id="upload_folder" placeholder="root" class="bg-gray-700 text-white text-xs px-2 py-1 rounded border border-gray-600 focus:border-blue-500 w-32">
                </div>
                <button onclick="document.getElementById('file_input').click()" class="bg-blue-600 hover:bg-blue-500 px-4 py-2 rounded text-sm font-semibold transition shadow">Browse Files</button>
            </div>
            <input type="file" id="file_input" multiple class="hidden">
        </div>

        <div class="flex-1 overflow-y-auto p-4 content-start scroller">
            <div class="grid grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6 gap-4" id="gallery_grid"></div>
        </div>
    </div>

    <!-- Right Pane: Editor & YOLO -->
    <div class="flex-1 bg-gray-800 flex flex-col h-full shadow-xl min-w-[320px] overflow-hidden relative">
        <div class="p-4 border-b border-gray-700 flex justify-between items-center flex-shrink-0 z-10 bg-gray-800">
            <h2 class="text-xl font-bold flex items-center gap-3">
                Metadata Editor
                <span id="save_indicator" class="text-xs font-bold px-2 py-1 rounded bg-gray-900 text-gray-500 hidden transition-colors"></span>
            </h2>
            <button id="btn_delete" onclick="deleteCurrentFile()" class="hidden text-red-400 hover:text-red-300 transition text-sm flex items-center">Delete File</button>
        </div>

        <div id="editor_panel" class="p-4 flex flex-col opacity-50 pointer-events-none transition-opacity border-b border-gray-700 pb-6 flex-1 overflow-y-auto scroller">
            <div class="flex justify-between items-center border-b border-gray-700 pb-2 mb-2 flex-shrink-0">
                <p id="selected_filename" class="text-sm font-mono text-blue-400 truncate w-3/4" title="Path">No file selected</p>
                <button onclick="moveCurrentFile()" class="text-[10px] bg-gray-700 hover:bg-gray-600 px-2 py-1 rounded shadow transition uppercase font-bold tracking-wider">Move</button>
            </div>
            
            <div id="canvas_container" class="bg-black rounded shadow w-full relative border border-gray-700 overflow-hidden min-h-[200px] resizable-vertical" style="flex: 1 1 auto;">
                <canvas id="media_canvas" class="absolute"></canvas>
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
                <input type="text" id="meta_tags" oninput="triggerAutosave()" placeholder="e.g. christmas tree, beach" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white focus:border-blue-500">
            </div>

            <div class="mt-4 flex-shrink-0">
                <div class="flex justify-between items-center mb-1">
                    <label class="block text-sm font-bold text-gray-300">Description (XMP-dc:Description)</label>
                    <button onclick="runAutoDescribe()" id="btn_autodescribe" class="text-xs bg-yellow-600 hover:bg-yellow-500 text-white px-2 py-1 rounded shadow transition flex items-center gap-1">
                        ✨ Auto-Describe
                    </button>
                </div>
                <textarea id="meta_desc" oninput="triggerAutosave()" class="w-full p-2 bg-gray-700 rounded border border-gray-600 text-white focus:border-blue-500 resize-y min-h-[80px]" placeholder="Description..."></textarea>
            </div>
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

    <!-- Dedup Modal -->
    <div id="dedup_modal" class="hidden absolute inset-0 bg-black bg-opacity-80 flex flex-col items-center justify-center z-50 p-6">
        <div class="bg-gray-800 p-6 rounded-lg shadow-xl w-full max-w-5xl h-[80vh] flex flex-col border border-gray-600">
            <div class="flex justify-between items-center mb-4 flex-shrink-0 border-b border-gray-700 pb-4">
                <div>
                    <h2 class="text-2xl font-bold text-indigo-400">Duplicate Images Found</h2>
                    <p class="text-sm text-gray-400 mt-1">Review duplicates below. Click "Keep & Merge" to automatically combine metadata tags and regions into a master image while trashing the rest.</p>
                </div>
                <button onclick="document.getElementById('dedup_modal').classList.add('hidden')" class="bg-gray-700 hover:bg-gray-600 px-6 py-2 rounded font-bold transition">Done</button>
            </div>
            <div id="dedup_content" class="flex-1 overflow-y-auto space-y-6 pt-2 pr-2">
                <!-- Groups dynamically inserted here -->
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
        let libraryData = [];
        
        // Canvas Setup
        const canvas = document.getElementById('media_canvas');
        const ctx = canvas.getContext('2d');
        let currentImgObj = new Image();
        let drawing = false;
        let startX = 0, startY = 0, curX = 0, curY = 0, pendingBox = null;
        let hasLoadedSettings = false;
        let autosaveTimeout = null;

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
            libraryData = data.files;
            filterGallery();
        }

        function filterGallery() {
            const q = document.getElementById('search_input').value.toLowerCase().trim();
            const filtered = q ? libraryData.filter(item => 
                item.filename.toLowerCase().includes(q) ||
                item.description.toLowerCase().includes(q) ||
                item.tags.some(t => t.toLowerCase().includes(q))
            ) : libraryData;

            document.getElementById('file_count').innerText = `${filtered.length} Items`;
            const grid = document.getElementById('gallery_grid');
            grid.innerHTML = '';
            
            filtered.forEach(item => {
                const f = item.filename;
                const safeId = f.replace(/[^a-zA-Z0-9]/g, '_');
                const div = document.createElement('div');
                div.className = `gallery-item bg-gray-800 border-2 border-transparent rounded overflow-hidden group`;
                div.id = `thumb_${safeId}`;
                div.onclick = () => selectFile(f);
                
                const img = document.createElement('img');
                img.src = '/api/thumb/' + encodeURIComponent(f);
                img.loading = "lazy";
                img.className = "absolute inset-0 w-full h-full object-cover pointer-events-none"; 
                
                let tagHtml = item.tags.length > 0 ? `<div class="absolute top-1 right-1 bg-blue-600 text-white text-[9px] font-bold px-1.5 py-0.5 rounded shadow">${item.tags.length} tags</div>` : '';
                
                const label = document.createElement('div');
                label.className = "absolute bottom-0 w-full bg-black bg-opacity-80 text-[10px] truncate px-2 py-1 text-center opacity-0 group-hover:opacity-100 pointer-events-none transition-opacity";
                label.innerText = f.split('/').pop(); 
                
                div.appendChild(img);
                if(tagHtml) div.insertAdjacentHTML('beforeend', tagHtml);
                div.appendChild(label); 
                grid.appendChild(div);
            });
            if (currentFile) document.getElementById(`thumb_${currentFile.replace(/[^a-zA-Z0-9]/g, '_')}`)?.classList.add('selected-item');
        }

        async function selectFile(filename) {
            currentFile = filename;
            document.querySelectorAll('.gallery-item').forEach(el => el.classList.remove('selected-item'));
            document.getElementById(`thumb_${filename.replace(/\\./g, '_')}`)?.classList.add('selected-item');
            
            document.getElementById('selected_filename').innerText = filename;
            document.getElementById('editor_panel').classList.remove('opacity-50', 'pointer-events-none');
            document.getElementById('yolo_controls').classList.remove('opacity-50', 'pointer-events-none');
            document.getElementById('btn_delete').classList.remove('hidden');
            
            // Reset indicators
            document.getElementById('save_indicator').classList.add('hidden');
            
            currentImgObj.src = '/api/file/' + encodeURIComponent(filename) + '?ts=' + Date.now();
            
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

        function triggerAutosave() {
            if (!currentFile) return;
            const ind = document.getElementById('save_indicator');
            ind.classList.remove('hidden', 'text-green-400', 'text-gray-500');
            ind.classList.add('text-yellow-400');
            ind.innerText = "Saving...";
            
            clearTimeout(autosaveTimeout);
            autosaveTimeout = setTimeout(() => {
                saveMetadata();
            }, 1000);
        }

        async function saveMetadata() {
            if(!currentFile) return;
            const tags = document.getElementById('meta_tags').value.split(',').map(s => s.trim()).filter(s => s);
            const desc = document.getElementById('meta_desc').value;
            const target = currentFile;
            
            const res = await fetch('/api/metadata', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({
                    action: 'write', filename: target, 
                    tags: tags, description: desc, regions: currentRegions
                })
            });
            const data = await res.json();
            if(data.success) {
                const libItem = libraryData.find(i => i.filename === target);
                if(libItem) { libItem.tags = tags; libItem.description = desc; }
                
                const ind = document.getElementById('save_indicator');
                ind.classList.remove('text-yellow-400');
                ind.classList.add('text-green-400');
                ind.innerText = "✓ Saved";
                setTimeout(() => { 
                    if(ind.innerText === "✓ Saved") {
                        ind.classList.remove('text-green-400');
                        ind.classList.add('text-gray-500');
                    }
                }, 2000);
            }
        }

        async function moveCurrentFile() {
            if(!currentFile) return;
            const dirSplit = currentFile.split('/');
            const currentDir = dirSplit.length > 1 ? dirSplit.slice(0, -1).join('/') : "";
            
            const newPath = prompt("Enter new folder path for this file (leave blank for root media directory):", currentDir);
            if(newPath === null) return;
            
            const res = await fetch('/api/move', {
                method: 'POST', headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({filename: currentFile, new_folder: newPath})
            });
            const data = await res.json();
            if(data.success) {
                currentFile = null;
                document.getElementById('editor_panel').classList.add('opacity-50', 'pointer-events-none');
                loadGallery();
            } else { alert("Failed to move file."); }
        }

        async function deleteCurrentFile() {
            if(!currentFile) return;
            if(confirm(`Delete ${currentFile.split('/').pop()}?`)) {
                await fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename: currentFile}) });
                currentFile = null; document.getElementById('editor_panel').classList.add('opacity-50', 'pointer-events-none');
                document.getElementById('save_indicator').classList.add('hidden');
                loadGallery();
            }
        }

        // --- Dedup System Client Logic ---
        async function runDedup() {
            const btn = document.getElementById('btn_dedup');
            const ogText = btn.innerHTML;
            btn.innerHTML = `Wait...`;
            btn.disabled = true;

            try {
                const res = await fetch('/api/dedup', { method: 'POST' });
                const data = await res.json();
                
                if(data.success) {
                    if(data.groups.length === 0) {
                        alert("Great news! No duplicate images were found in your library.");
                    } else {
                        renderDedupGroups(data.groups);
                        document.getElementById('dedup_modal').classList.remove('hidden');
                    }
                }
            } catch(e) {
                alert("Error running the dedup scan.");
            }
            
            btn.innerHTML = ogText;
            btn.disabled = false;
        }

        function renderDedupGroups(groups) {
            const container = document.getElementById('dedup_content');
            container.innerHTML = '';
            groups.forEach((group, idx) => {
                let html = `<div class="bg-gray-750 border border-gray-700 p-4 rounded-lg shadow-lg mb-4" id="dedup_group_${idx}">
                    <h3 class="font-bold text-gray-300 mb-3 border-b border-gray-700 pb-2">Similarity Group ${idx+1}</h3>
                    <div class="flex gap-4 overflow-x-auto pb-2 scroller">`;
                
                const groupStr = JSON.stringify(group).replace(/"/g, '&quot;');
                
                group.forEach(file => {
                    const safeId = file.replace(/[^a-zA-Z0-9]/g, '_');
                    html += `
                    <div class="flex flex-col items-center flex-shrink-0 w-40 bg-gray-800 p-2 rounded border border-gray-700 relative group" id="dedup_item_${safeId}">
                        <img src="/api/thumb/${encodeURIComponent(file)}" class="w-36 h-36 object-cover rounded mb-2 border border-gray-600 bg-black">
                        <p class="text-[10px] truncate w-full text-center text-gray-400 mb-2 font-mono" title="${file}">${file.split('/').pop()}</p>
                        <div class="flex flex-col w-full gap-1">
                            <button onclick="keepAndMerge('${file}', '${groupStr}', ${idx})" class="w-full bg-green-600 hover:bg-green-500 text-xs font-bold px-2 py-1.5 rounded transition shadow">Keep & Merge</button>
                            <button onclick="deleteFromDedup('${file}', ${idx})" class="w-full bg-gray-700 hover:bg-red-600 text-gray-300 hover:text-white text-[10px] font-bold px-2 py-1 rounded transition">Delete Only</button>
                        </div>
                    </div>`;
                });
                
                html += `</div></div>`;
                container.innerHTML += html;
            });
        }

        async function keepAndMerge(target, groupStr, groupId) {
            const allFiles = JSON.parse(groupStr);
            const others = allFiles.filter(f => f !== target);
            
            if(others.length === 0) {
                alert("No other files to merge with.");
                return;
            }

            if(!confirm(`Keep ${target} and smartly merge metadata from ${others.length} other duplicate(s)?\\n\\nThe other files will be deleted automatically.`)) return;

            const res = await fetch('/api/dedup_merge', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({target: target, others: others})
            });
            
            const data = await res.json();
            if(data.success) {
                const groupEl = document.getElementById(`dedup_group_${groupId}`);
                if(groupEl) groupEl.remove();

                if(document.getElementById('dedup_content').children.length === 0) {
                    document.getElementById('dedup_modal').classList.add('hidden');
                }

                if(others.includes(currentFile)) {
                    currentFile = null;
                    document.getElementById('editor_panel').classList.add('opacity-50', 'pointer-events-none');
                } else if(currentFile === target) {
                    selectFile(target); // Reload merged metadata into editor
                }
                loadGallery();
            } else {
                alert("Error merging: " + data.error);
            }
        }

        async function deleteFromDedup(filename, groupId) {
            if(confirm(`Trash file ${filename.split('/').pop()} WITHOUT merging metadata?`)) {
                await fetch('/api/delete', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({filename: filename}) });
                
                const safeId = filename.replace(/[^a-zA-Z0-9]/g, '_');
                const el = document.getElementById(`dedup_item_${safeId}`);
                if(el) el.remove();
                
                // Hide group if only 1 remains
                const groupEl = document.getElementById(`dedup_group_${groupId}`);
                if(groupEl && groupEl.querySelectorAll('.flex-shrink-0').length <= 1) {
                    groupEl.remove();
                }

                // Close modal if completely empty
                if(document.getElementById('dedup_content').children.length === 0) {
                    document.getElementById('dedup_modal').classList.add('hidden');
                }
                
                if(currentFile === filename) {
                    currentFile = null;
                    document.getElementById('editor_panel').classList.add('opacity-50', 'pointer-events-none');
                }
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
                triggerAutosave();
            } else alert(data.error);
            btn.innerText = "Auto-Tag Current Image";
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
                    triggerAutosave();
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
            const targetFolder = document.getElementById('upload_folder').value.trim();
            for(let i=0; i<files.length; i++) {
                dropzone.innerHTML = `<p class="text-blue-400 font-bold animate-pulse">Converting ${i+1}/${files.length}...</p>`;
                let fd = new FormData(); 
                fd.append('file', files[i]);
                fd.append('folder', targetFolder);
                await fetch('/api/upload', { method: 'POST', body: fd });
            }
            dropzone.innerHTML = og; 
            document.getElementById('upload_folder').value = targetFolder;
            loadGallery();
        }

        // Canvas Handlers
        currentImgObj.onload = () => drawCanvas();
        window.addEventListener('resize', () => { if(currentFile) drawCanvas(); });

        // Dynamic Resizing Observer for the Container boundaries
        const canvasContainer = document.getElementById('canvas_container');
        const resizeObserver = new ResizeObserver(() => {
            if(currentFile && currentImgObj.width) {
                requestAnimationFrame(() => drawCanvas());
            }
        });
        if(canvasContainer) resizeObserver.observe(canvasContainer);

        function drawCanvas() {
            if(!currentImgObj.src || !currentImgObj.width) return;
            const aspect = currentImgObj.width / currentImgObj.height;
            const parent = canvas.parentElement;
            
            const parentW = parent.clientWidth;
            const parentH = parent.clientHeight;
            
            // Image object fitting logic (no scrolling)
            let drawW = parentW;
            let drawH = drawW / aspect;
            
            if (drawH > parentH) {
                drawH = parentH;
                drawW = drawH * aspect;
            }
            
            canvas.width = drawW; 
            canvas.height = drawH;
            
            // Center Canvas absolutely inside its flex wrapper
            canvas.style.left = `${(parentW - drawW) / 2}px`;
            canvas.style.top = `${(parentH - drawH) / 2}px`;
            
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
            if(!document.getElementById('toggle_regions').checked) return; 
            for (let i = currentRegions.length - 1; i >= 0; i--) {
                const b = currentRegions[i], pxW = b.w * canvas.width, pxH = b.h * canvas.height;
                const pxX = (b.cx * canvas.width) - pxW/2, pxY = (b.cy * canvas.height) - pxH/2;
                if (e.offsetX >= pxX && e.offsetX <= pxX+pxW && e.offsetY >= pxY && e.offsetY <= pxY+pxH) {
                    currentRegions.splice(i, 1); 
                    drawCanvas(); 
                    triggerAutosave(); 
                    break; 
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
            document.getElementById('region_modal').classList.add('hidden'); 
            drawCanvas();
            triggerAutosave();
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
    access_logger.info("Starting Background Indexer...")
    threading.Thread(target=build_metadata_index).start()
    
    access_logger.info("Starting Web Application on Port 8000...")
    app.run(host='0.0.0.0', port=8000, debug=False, threaded=True)