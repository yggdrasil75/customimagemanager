import os
import sys
import argparse
import requests

IMAGE_EXTENSIONS = {'.jpg', '.jpeg', '.png', '.webp', '.bmp', '.jxl'}

def load_classes(source_dir):
    """Attempt to load a classes.txt from the root of the source directory."""
    classes_path = os.path.join(source_dir, "classes.txt")
    if os.path.exists(classes_path):
        with open(classes_path, 'r', encoding='utf-8') as f:
            return [line.strip() for line in f.readlines() if line.strip()]
    return []

def parse_sidecar(filepath, classes_map):
    """
    Reads a .txt file. 
    Returns (regions_list, description_string, tags_list)
    """
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

def bulk_upload(source_dir, server_url):
    source_dir = os.path.abspath(source_dir)
    if not os.path.isdir(source_dir):
        print(f"Error: Source directory '{source_dir}' does not exist.")
        sys.exit(1)

    classes_map = load_classes(source_dir)
    if classes_map:
        print(f"[*] Found classes.txt with {len(classes_map)} classes.")
    else:
        print("[*] No classes.txt found. YOLO regions will default to class_0, class_1, etc.")

    # Gather all images
    upload_list = []
    for root, _, files in os.walk(source_dir):
        for f in files:
            ext = os.path.splitext(f)[1].lower()
            if ext in IMAGE_EXTENSIONS:
                upload_list.append(os.path.join(root, f))

    total = len(upload_list)
    print(f"[*] Found {total} images to upload.\n")

    upload_endpoint = f"{server_url.rstrip('/')}/api/upload"
    metadata_endpoint = f"{server_url.rstrip('/')}/api/metadata"

    for idx, filepath in enumerate(upload_list, 1):
        rel_dir = os.path.relpath(os.path.dirname(filepath), source_dir)
        target_folder = rel_dir.replace('\\', '/') if rel_dir != "." else ""
        filename = os.path.basename(filepath)
        
        print(f"[{idx}/{total}] Uploading: {target_folder + '/' + filename if target_folder else filename}")

        # 1. Upload the Image File
        try:
            with open(filepath, 'rb') as f:
                files = {'file': f}
                data = {'folder': target_folder}
                
                res = requests.post(upload_endpoint, files=files, data=data, timeout=60)
                res.raise_for_status()
                
                resp_data = res.json()
                if not resp_data.get('success'):
                    print(f"  [!] Server rejected upload for {filename}.")
                    continue
                
                # The server might have converted it to .jxl, so we grab the final filename from the response
                final_basename = resp_data.get('filename', filename)
                
        except Exception as e:
            print(f"  [!] Upload failed: {e}")
            continue

        # 2. Check for .txt Sidecar & Sync Metadata
        base_no_ext = os.path.splitext(filepath)[0]
        sidecar_path = base_no_ext + ".txt"
        
        regions, description, tags = parse_sidecar(sidecar_path, classes_map)
        
        # If we extracted regions, description, or tags, send the metadata update
        if regions or description or tags:
            target_filepath = f"{target_folder}/{final_basename}" if target_folder else final_basename
            
            meta_payload = {
                "action": "write",
                "filename": target_filepath,
                "tags": tags,
                "description": description,
                "regions": regions
            }
            
            try:
                meta_res = requests.post(metadata_endpoint, json=meta_payload, timeout=15)
                meta_res.raise_for_status()
                if regions:
                    print(f"  [+] Synced {len(regions)} YOLO regions.")
                if tags:
                    print(f"  [+] Synced {len(tags)} tags.")
                if description:
                    print(f"  [+] Synced text description.")
            except Exception as e:
                print(f"  [!] Failed to sync metadata: {e}")

    print("\n[*] Bulk upload complete!")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk Upload Tool for AI Media Manager")
    parser.add_argument("source_dir", help="Path to the local folder containing images and .txt sidecars")
    parser.add_argument("--url", default="http://localhost:8000", help="URL of the Media Manager server (default: http://localhost:8000)")
    
    args = parser.parse_args()
    
    bulk_upload(args.source_dir, args.url)