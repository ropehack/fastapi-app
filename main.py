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

# ■ 一覧（検索 + ページング）
@app.get("/companies")
def get_companies(area: str = "", keyword: str = "", limit: int = 10, offset: int = 0):

    query = supabase.table("companies").select("*")

    if area:
        query = query.eq("area", area)

    if keyword:
        query = query.ilike("name", f"%{keyword}%")

    res = query.range(offset, offset + limit - 1).execute()

    return res.data


# ■ 追加（重複防止）
@app.post("/company")
def create_company(data: dict):

    exists = supabase.table("companies") \
        .select("id") \
        .eq("name", data.get("name")) \
        .eq("area", data.get("area")) \
        .execute()

    if len(exists.data) == 0:
        supabase.table("companies").insert({
            "name": data.get("name"),
            "area": data.get("area"),
            "email": data.get("email", ""),
            "phone": data.get("phone", ""),
            "url": data.get("url", ""),
            "status": "未対応"
        }).execute()

    return {"ok": True}


# ■ 削除（完全版）
@app.delete("/company/{id}")
def delete_company(id: int):
    supabase.table("companies").delete().eq("id", id).execute()
    return {"ok": True}


# ■ ステータス変更
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

    return {"ok": True}
