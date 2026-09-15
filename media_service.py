import os
import time
import uuid
import threading
from concurrent.futures import ThreadPoolExecutor

import requests

BASE_URL = os.getenv("PLUSVIBE_BASE", "https://plusvibeapi.ru").rstrip("/")
API_KEY = os.getenv("PLUSVIBE_API_KEY", "").strip()
CATALOG_TTL = 300
POLL_INITIAL = 2.0
POLL_MAX = 12.0
KIND_ALIASES = {
    "image":"image","images":"image","photo":"image","фото":"image","изображение":"image",
    "video":"video","videos":"video","ролик":"video","видео":"video",
    "tts":"tts","voice":"tts","голос":"tts","озвучка":"tts",
    "music":"music","audio":"music","музыка":"music",
    "3d":"3d","model":"3d","модель":"3d",
}
_catalog={"items":[],"at":0.0}; _catalog_lock=threading.RLock()
_jobs={}; _jobs_lock=threading.RLock()
_workers=max(2,min(8,int(os.getenv("MEDIA_WORKERS","3"))))
_executor=ThreadPoolExecutor(max_workers=_workers,thread_name_prefix="bulba-media")
_session=requests.Session()

def configured(): return bool(API_KEY)
def normalize_kind(kind): return KIND_ALIASES.get(str(kind or "").strip().lower())
def _headers(): return {"Authorization":f"Bearer {API_KEY}","Content-Type":"application/json"}

def _catalog_items(raw):
    data=raw if isinstance(raw,dict) else {}; items=data.get("data",data.get("models",data.get("items",[])))
    if isinstance(items,dict): items=items.get("items",[])
    out=[]
    for item in items if isinstance(items,list) else []:
        if not isinstance(item,dict): continue
        mid=str(item.get("id") or item.get("model") or item.get("name") or "").strip(); kind=normalize_kind(item.get("kind") or item.get("type"))
        if not mid or not kind: continue
        params=item.get("params") if isinstance(item.get("params"),dict) else {}
        variants=item.get("variants") if isinstance(item.get("variants"),list) else []
        price=item.get("fromRub")
        try: price=float(price) if price is not None else None
        except (TypeError,ValueError): price=None
        out.append({"id":mid,"name":str(item.get("name") or mid),"kind":kind,"params":params,"variants":variants,"fromRub":price,"priceUnavailable":bool(item.get("priceUnavailable",False))})
    return out

def get_catalog(force=False):
    now=time.time()
    with _catalog_lock:
        if not force and now-_catalog["at"]<CATALOG_TTL and _catalog["items"]: return list(_catalog["items"])
    if not configured(): return []
    try:
        r=_session.get(f"{BASE_URL}/api/media-catalog",headers=_headers(),timeout=20); r.raise_for_status(); items=_catalog_items(r.json())
        with _catalog_lock: _catalog["items"],_catalog["at"]=items,now
        return list(items)
    except Exception as exc:
        print("PlusVibe catalog error:",repr(exc));
        with _catalog_lock: return list(_catalog["items"])

def models_for_kind(kind):
    k=normalize_kind(kind); return [x for x in get_catalog() if x["kind"]==k]

def choose_model(kind,requested=None):
    k=normalize_kind(kind); requested=str(requested or "").strip(); items=models_for_kind(k)
    if not k: raise ValueError("Неизвестный тип медиа.")
    if requested:
        for item in items:
            if item["id"]==requested: return item
        raise ValueError("Выбранная медиа-модель недоступна.")
    if not items: raise RuntimeError("PlusVibe не вернул доступные модели для этого типа медиа.")
    priced=[x for x in items if x["fromRub"] is not None and not x["priceUnavailable"]]
    return min(priced or items,key=lambda x:(x["fromRub"] is None,x["fromRub"] or 0,x["id"]))

def _job(job_id):
    with _jobs_lock: return _jobs.get(str(job_id))
def job_status(job_id):
    item=_job(job_id); return dict(item) if item else None

def _set_job(job_id,**changes):
    with _jobs_lock:
        item=_jobs.get(str(job_id))
        if not item: return None
        item.update(changes); item["updated_at"]=int(time.time()); return dict(item)

def _request_generate(model,prompt,opts):
    r=_session.post(f"{BASE_URL}/api/media/generate",headers=_headers(),json={"model":model,"prompt":prompt,"opts":opts or {}},timeout=35)
    try: data=r.json()
    except Exception: data={"message":r.text[:500]}
    if not r.ok: raise RuntimeError(str(data.get("message") or data.get("error") or f"PlusVibe HTTP {r.status_code}"))
    provider_job=data.get("jobId") or data.get("job_id")
    if provider_job: return str(provider_job),data
    return None,{"status":"success","resultUrls":data.get("resultUrls") or data.get("urls") or [],"data":data}

def _poll_provider(job_id,timeout):
    deadline=time.time()+timeout; delay=POLL_INITIAL
    while time.time()<deadline:
        r=_session.get(f"{BASE_URL}/api/media/jobs/{job_id}",headers=_headers(),timeout=25)
        if not r.ok: raise RuntimeError(f"PlusVibe status HTTP {r.status_code}")
        data=r.json() if r.content else {}; status=str(data.get("status") or "processing").lower()
        if status in {"success","completed","done"}: return data
        if status in {"fail","failed","error","cancelled"}: raise RuntimeError(str(data.get("failMsg") or data.get("error") or "Медиа-задача завершилась ошибкой."))
        time.sleep(delay); delay=min(POLL_MAX,delay*1.6)
    raise TimeoutError("Медиа-задача выполняется дольше допустимого времени.")

def _timeout_for_kind(kind): return {"image":120,"video":660,"tts":180,"music":300,"3d":360}.get(kind,300)
def _normalize_result(kind,model,data):
    urls=data.get("resultUrls") or data.get("urls") or data.get("result_urls") or []
    if isinstance(urls,str): urls=[urls]
    return {"status":"success","kind":kind,"model":model,"urls":[str(x) for x in urls if x],"text":str(data.get("text") or data.get("transcript") or "")}

def _run_job(job_id,kind,prompt,requested_model,opts):
    try:
        selected=choose_model(kind,requested_model); model=selected["id"]; _set_job(job_id,status="submitting",model=model)
        provider_job,immediate=_request_generate(model,prompt,opts)
        if provider_job:
            _set_job(job_id,status="processing",provider_job_id=provider_job); result=_poll_provider(provider_job,_timeout_for_kind(kind))
        else: result=immediate
        final=_normalize_result(kind,model,result)
        if not final["urls"] and kind!="tts": raise RuntimeError("PlusVibe завершил задачу без результата.")
        _set_job(job_id,**final)
    except Exception as exc:
        print("Media job error:",repr(exc)); _set_job(job_id,status="failed",error=str(exc)[:1000])

def create_job(kind,prompt,model=None,opts=None,user_id=None):
    kind=normalize_kind(kind)
    if not kind: raise ValueError("Неизвестный тип медиа.")
    if not configured(): raise RuntimeError("PLUSVIBE_API_KEY не настроен.")
    prompt=str(prompt or "").strip()
    if not prompt: raise ValueError("Опиши, что нужно создать.")
    if len(prompt)>12000: raise ValueError("Описание слишком длинное.")
    selected=choose_model(kind,model); job_id=uuid.uuid4().hex; now=int(time.time())
    with _jobs_lock: _jobs[job_id]={"job_id":job_id,"user_id":str(user_id or ""),"kind":kind,"model":selected["id"],"status":"queued","created_at":now,"updated_at":now}
    _executor.submit(_run_job,job_id,kind,prompt,model,dict(opts or {})); return job_id,selected

def cleanup_jobs(max_age=86400):
    cutoff=time.time()-max_age
    with _jobs_lock:
        for jid,item in list(_jobs.items()):
            if item.get("updated_at",item.get("created_at",0))<cutoff: _jobs.pop(jid,None)

def claim_success(job_id):
    with _jobs_lock:
        item=_jobs.get(str(job_id))
        if not item or item.get("status")!="success": return None
        if item.get("license_claimed"): return dict(item)
        item["license_claimed"]=True; return dict(item)
