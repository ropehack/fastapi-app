import os
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

supabase = create_client(
    os.getenv("SUPABASE_URL"),
    os.getenv("SUPABASE_KEY")
)

# 一覧取得
@app.get("/companies")
def get_companies():
    return supabase.table("companies").select("*").execute().data

# 追加
@app.get("/search")
def search(area: str = "", keyword: str = ""):
    supabase.table("companies").insert({
        "name": keyword,
        "area": area,
        "email": "",
        "phone": "",
        "url": "",
        "status": "未対応"
    }).execute()
    return {"ok": True}

# 削除
@app.delete("/company/{id}")
def delete_company(id: int):
    supabase.table("companies").delete().eq("id", id).execute()
    return {"ok": True}

# ステータス変更（クリックで循環）
@app.post("/status/{id}")
def update_status(id: int):

    res = supabase.table("companies") \
        .select("status") \
        .eq("id", id) \
        .single() \
        .execute()

    current = res.data["status"] if res.data else "未対応"

    flow = ["未対応", "連絡済み", "商談中", "成約"]

    if current not in flow:
        next_status = "未対応"
    else:
        idx = flow.index(current)
        next_status = flow[(idx + 1) % len(flow)]

    supabase.table("companies").update({
        "status": next_status
    }).eq("id", id).execute()

    return {"ok": True, "status": next_status}
