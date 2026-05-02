import os
import sys
import argparse
import requests
import json
from concurrent.futures import ThreadPoolExecutor, as_completed

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.jxl'}

def load_classes(source_dir):
    classes_path = os.path.join(source_dir, "classes.txt")
    if os.path.exists(classes_path):
        with open(classes_path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return []

def parse_sidecar(filepath, classes_map):
    if not os.path.exists(filepath):
        return [], "", []

    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read().strip()
    except Exception as e:
        print(f"  [!] Failed to read sidecar {filepath}: {e}")
        return [], "", []
        
    if not content:
        return [], "", []

    # Check 1: Pipe-separated Tags
    if content.count('|') > 1:
        raw_tags = [t.strip() for t in content.split('|') if t.strip()]
        tags = []
        desc_parts = []
        
        for t in raw_tags:
            t_lower = t.lower()
            if t_lower.startswith('description:'):
                # Strip the 'description:' prefix and any leading/trailing whitespace
                clean_desc = t[12:].strip()
                if clean_desc:
                    desc_parts.append(clean_desc)
            elif len(t) > 20:
                desc_parts.append(t)
            else:
                tags.append(t)
                
        description = "; ".join(desc_parts)
        return [], description, tags
        
    # Check 2: Try parsing strictly as YOLO format
    lines = content.split('\n')
    is_yolo = True
    regions = []
    
    # Try parsing strictly as YOLO format
    for line in lines:
        line = line.strip()
        if not line:
            continue
            
        parts = line.split()
        if len(parts) != 5:
            is_yolo = False
            break
            
        try:
            cls_id = int(parts[0])
            cx, cy, w, h = float(parts[1]), float(parts[2]), float(parts[3]), float(parts[4])
            
            # YOLO normalized coords must be between 0.0 and 1.0
            if not (0.0 <= cx <= 1.0 and 0.0 <= cy <= 1.0 and 0.0 <= w <= 1.0 and 0.0 <= h <= 1.0):
                is_yolo = False
                break
                
            cls_name = classes_map[cls_id] if cls_id < len(classes_map) else f"class_{cls_id}"
            regions.append({
                "class_name": cls_name,
                "cx": cx, "cy": cy, "w": w, "h": h
            })
        except ValueError:
            is_yolo = False
            break
            
    if is_yolo and regions:
        return regions, "", []
    else:
        # Fallback: Treat the whole file as a description
        return [], content, []

def process_file(filepath, source_dir, classes_map, upload_endpoint):
    rel_dir = os.path.relpath(os.path.dirname(filepath), source_dir)
    target_folder = rel_dir.replace('\\', '/') if rel_dir != "." else ""
    filename = os.path.basename(filepath)
    
    base_no_ext = os.path.splitext(filepath)[0]
    sidecar_path = base_no_ext + ".txt"
    
    regions, description, tags = parse_sidecar(sidecar_path, classes_map)
    
    # Bundle metadata with the upload request to save a network trip
    metadata_payload = {}
    if regions or description or tags:
        metadata_payload = {
            "tags": tags,
            "description": description,
            "regions": regions
        }

    try:
        with open(filepath, 'rb') as f:
            files = {'file': f}
            data = {'folder': target_folder}
            if metadata_payload:
                data['metadata'] = json.dumps(metadata_payload)
            
            res = requests.post(upload_endpoint, files=files, data=data, timeout=120)
            res.raise_for_status()
            
            resp_data = res.json()
            if not resp_data.get('success'):
                return False, f"Server rejected {filename}"
            return True, f"Uploaded {filename}"
    except Exception as e:
        return False, f"Failed {filename}: {str(e)}"

def bulk_upload(source_dir, server_url, workers=8):
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        print(f"Error: Source directory '{source_dir}' does not exist.")
        sys.exit(1)

    classes_map = load_classes(source_dir)
    upload_list = [os.path.join(root, f) for root, _, files in os.walk(source_dir) for f in files if os.path.splitext(f)[1].lower() in IMAGE_EXTENSIONS]

    total = len(upload_list)
    print(f"[*] Found {total} images to upload.\n")

    upload_endpoint = f"{server_url.rstrip('/')}/api/upload"
    success_count = 0

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(process_file, fp, source_dir, classes_map, upload_endpoint): fp for fp in upload_list}
        
        for idx, future in enumerate(as_completed(futures), 1):
            success, msg = future.result()
            if success: success_count += 1
            print(f"[{idx}/{total}] {msg}")

    print("\n[*] Bulk upload complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_dir", help="Path to local folder")
    parser.add_argument("--url", default="http://localhost:8000", help="URL of Media Manager (default: http://localhost:8000)")
    parser.add_argument("--workers", type=int, default=8, help="Number of concurrent uploads")
    args = parser.parse_args()
    bulk_upload(args.source_dir, args.url, args.workers)