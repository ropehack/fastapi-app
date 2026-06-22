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

# 一覧（10件ずつ）
@app.get("/companies")
def get_companies(limit: int = 10, offset: int = 0):

    res = supabase.table("companies") \
        .select("*") \
        .range(offset, offset + limit - 1) \
        .execute()

    return res.data


# 追加（重複防止あり）
@app.get("/search")
def search(area: str = "", keyword: str = ""):

    exists = supabase.table("companies") \
        .select("id") \
        .eq("name", keyword) \
        .eq("area", area) \
        .execute()

    if len(exists.data) == 0:
        supabase.table("companies").insert({
            "name": keyword,
            "area": area,
            "email": "",
            "phone": "",
            "url": "",
            "status": "未対応"
        }).execute()

    return {"ok": True}


# 削除（完全版）
@app.delete("/company/{id}")
def delete_company(id: int):
    supabase.table("companies") \
        .delete() \
        .eq("id", id) \
        .execute()

    return {"ok": True}


# ステータス切替
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
