import os
import shutil
import json
import time
import threading
import subprocess
import sys
import traceback
import yaml
from flask import Flask, request, jsonify, send_file

app = Flask(__name__)
WORKSPACE = os.path.abspath("worker_workspace")
os.makedirs(WORKSPACE, exist_ok=True)

jobs = {}

def yolo_train_thread(job_id, dataset_path, config):
    job_dir = os.path.join(WORKSPACE, job_id)
    log_file = os.path.join(job_dir, "training.log")
    
    try:
        yaml_path = os.path.join(dataset_path, "dataset.yaml")
        with open(yaml_path, "r") as f:
            yaml_data = yaml.safe_load(f)
        
        yaml_data["path"] = dataset_path
        
        with open(yaml_path, "w") as f:
            yaml.dump(yaml_data, f)
        base_model = config.get("base_model", "yolo11m.pt")
        epochs = config.get("epochs", 100)
        batch = config.get("batch", 4)
        imgsz = config.get("imgsz", 640)
        device = config.get("device", "-1")

        # Passing arguments via sys.argv prevents Windows path slash (\) unicode escape errors
        patch_script = """
import sys
import os
import torch
from ultralytics import YOLO

# 1. Force PyTorch to utilize all available CPU cores for math operations
num_cores = os.cpu_count() or 1
torch.set_num_threads(num_cores)

yaml_path = sys.argv[1]
base_model = sys.argv[2]
epochs = int(sys.argv[3])
batch = int(sys.argv[4])
imgsz = int(sys.argv[5])
device = sys.argv[6]
project_dir = sys.argv[7]

if device == "-1": device = -1
elif device.isdigit(): device = int(device)

model = YOLO(base_model)
# Note: project specifies where 'runs' goes. We keep it inside the job folder.
model.train(data=yaml_path, epochs=epochs, batch=batch, imgsz=imgsz, device=device, project=project_dir, name="run", cache='ram', save_period=-1, plots=False, workers=num_cores)
"""
        script_path = os.path.join(job_dir, "train_script.py")
        with open(script_path, "w") as f:
            f.write(patch_script)

        with open(log_file, "w", encoding="utf-8") as lf:
            lf.write(f"--- Starting remote job: {job_id} ---\n")
            lf.flush()
            
            # 3. Force OpenBLAS/OpenMP backend thread allocation
            env = os.environ.copy()
            total_cores = str(os.cpu_count() or 1)
            env["OMP_NUM_THREADS"] = total_cores
            env["OPENBLAS_NUM_THREADS"] = total_cores
            env["MKL_NUM_THREADS"] = total_cores
            
            # Execute script with safe arguments and overridden environment variables
            cmd = [
                sys.executable, 
                script_path, 
                yaml_path, 
                base_model, 
                str(epochs), 
                str(batch), 
                str(imgsz), 
                str(device), 
                job_dir
            ]
            
            process = subprocess.Popen(cmd, stdout=lf, stderr=subprocess.STDOUT, env=env)
            process.wait()
            
            if process.returncode != 0:
                raise Exception("YOLO Subprocess returned a non-zero exit code.")
        
        # Locate the resulting best.pt
        best_pt = os.path.join(job_dir, "run", "weights", "best.pt")
        if os.path.exists(best_pt):
            jobs[job_id]["status"] = "completed"
            jobs[job_id]["best_pt"] = best_pt
        else:
            raise Exception("best.pt not found. Training may have failed silently.")
            
    except Exception as e:
        jobs[job_id]["status"] = "failed"
        # error_details = traceback.format_exc()
        # with open(log_file, "a", encoding="utf-8") as lf:
        #     lf.write(f"\n[ERROR] Thread failed: {str(e)}\n")
        #     lf.write(f"Traceback:\n{error_details}\n")

@app.route('/api/start_train', methods=['POST'])
def start_train():
    if 'dataset' not in request.files or 'config' not in request.form:
        return jsonify({"error": "Missing dataset or config"}), 400

    job_id = f"job_{int(time.time())}"
    job_dir = os.path.join(WORKSPACE, job_id)
    os.makedirs(job_dir, exist_ok=True)
    
    zip_file = request.files['dataset']
    zip_path = os.path.join(job_dir, "dataset.zip")
    zip_file.save(zip_path)
    
    dataset_path = os.path.join(job_dir, "dataset")
    shutil.unpack_archive(zip_path, extract_dir=dataset_path)
    
    config = json.loads(request.form['config'])
    
    jobs[job_id] = {
        "status": "running",
        "log_file": os.path.join(job_dir, "training.log"),
        "best_pt": None
    }
    
    threading.Thread(target=yolo_train_thread, args=(job_id, dataset_path, config)).start()
    
    return jsonify({"job_id": job_id})

@app.route('/api/status/<job_id>', methods=['GET'])
def check_status(job_id):
    if job_id not in jobs:
        return jsonify({"error": "Job not found"}), 404
        
    job_info = jobs[job_id]
    log_data = "Log file not found."
    if os.path.exists(job_info["log_file"]):
        with open(job_info["log_file"], "r", encoding="utf-8", errors="replace") as f:
            # Send the last 200 lines to save bandwidth
            lines = f.readlines()
            log_data = "".join(lines[-200:])
            
    return jsonify({
        "status": job_info["status"],
        "log": log_data
    })

@app.route('/api/download/<job_id>', methods=['GET'])
def download_model(job_id):
    if job_id not in jobs or jobs[job_id]["status"] != "completed":
        return jsonify({"error": "Model not ready"}), 400
        
    return send_file(jobs[job_id]["best_pt"], as_attachment=True, download_name="best.pt")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)