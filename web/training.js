let hasIp=false;
async function poll(){
  try{
    const s=await fetch('/api/state').then(r=>r.json());
    document.getElementById('app_status').innerText=s.status_text;
    if(!hasIp){document.getElementById('remote_ip').value=s.remote_ip;hasIp=true;}
    const le=document.getElementById('log_output');
    const atB=le.scrollHeight-le.clientHeight<=le.scrollTop+60;
    le.innerText=(await fetch('/api/training_log').then(r=>r.json())).log;
    if(atB) le.scrollTop=le.scrollHeight;
  }catch(e){}
}
function startTraining(){
  fetch('/api/train',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({base_model:document.getElementById('base_model').value,
      epochs:document.getElementById('epochs').value,batch:document.getElementById('batch').value,
      imgsz:document.getElementById('imgsz').value,device:document.getElementById('device').value,
      remote_ip:document.getElementById('remote_ip').value.trim()})});
  alert('Job sent!');
}
setInterval(poll,1500); poll();