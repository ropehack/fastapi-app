import os
import logging
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel, EmailStr
from typing import Optional

# ============================================================
# ログ設定（Renderのログで問題を追いやすくする）
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="営業リスト管理API", version="1.0.0")

# ============================================================
# CORS設定
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ropehack.jp",
        "http://localhost:3000",   # ローカル開発用
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ============================================================
# Supabase接続
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL または SUPABASE_KEY が設定されていません")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info(f"Supabase接続完了: {SUPABASE_URL}")

# ============================================================
# ステータスフロー定義
# ============================================================
STATUS_FLOW = ["未対応", "連絡済み", "商談中", "成約"]

# ============================================================
# Pydanticモデル（型安全 + バリデーション）
# ============================================================
class CompanyCreate(BaseModel):
    name: str
    area: Optional[str] = ""
    email: Optional[str] = ""
    phone: Optional[str] = ""
    url: Optional[str] = ""

class CompanyUpdate(BaseModel):
    name: Optional[str] = None
    area: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    url: Optional[str] = None
    status: Optional[str] = None

# ============================================================
# ヘルスチェック
# ============================================================
@app.get("/")
def health_check():
    return {"status": "ok", "message": "営業リスト管理API 稼働中"}

@app.get("/health")
def health():
    """DB接続確認用エンドポイント"""
    try:
        res = supabase.table("companies").select("id").limit(1).execute()
        return {"status": "ok", "db": "connected", "sample_count": len(res.data)}
    except Exception as e:
        logger.error(f"DB接続エラー: {e}")
        raise HTTPException(status_code=503, detail=f"DB接続失敗: {str(e)}")

# ============================================================
# 一覧・検索（SELECT）
# ============================================================
@app.get("/companies")
def get_companies(
    area: str = Query(default="", description="エリアでフィルタ"),
    keyword: str = Query(default="", description="会社名部分一致検索"),
    status: str = Query(default="", description="ステータスでフィルタ"),
    limit: int = Query(default=10, ge=1, le=100, description="取得件数"),
    offset: int = Query(default=0, ge=0, description="オフセット"),
):
    try:
        query = supabase.table("companies").select("*", count="exact")

        if area:
            query = query.eq("area", area)

        if keyword:
            # 部分一致：name OR email OR phone を横断検索
            # Supabaseでのor検索
            query = query.or_(
                f"name.ilike.%{keyword}%,"
                f"email.ilike.%{keyword}%,"
                f"phone.ilike.%{keyword}%"
            )

        if status:
            query = query.eq("status", status)

        # 新しい順に並べる
        query = query.order("id", desc=True)

        # ページング
        res = query.range(offset, offset + limit - 1).execute()

        logger.info(f"検索結果: {len(res.data)}件 (area={area}, keyword={keyword}, status={status})")

        return {
            "data": res.data,
            "total": res.count,
            "limit": limit,
            "offset": offset,
        }

    except Exception as e:
        logger.error(f"companies SELECT エラー: {e}")
        raise HTTPException(status_code=500, detail=f"データ取得失敗: {str(e)}")


# ============================================================
# 1件取得
# ============================================================
@app.get("/company/{id}")
def get_company(id: int):
    try:
        res = supabase.table("companies").select("*").eq("id", id).single().execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")
        return res.data
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"company GET エラー (id={id}): {e}")
        raise HTTPException(status_code=500, detail=f"データ取得失敗: {str(e)}")


# ============================================================
# 追加（INSERT）
# ============================================================
@app.post("/company", status_code=201)
def add_company(data: CompanyCreate):
    try:
        payload = {
            "name": data.name.strip(),
            "area": data.area.strip() if data.area else "",
            "email": data.email.strip() if data.email else "",
            "phone": data.phone.strip() if data.phone else "",
            "url": data.url.strip() if data.url else "",
            "status": "未対応",
        }

        if not payload["name"]:
            raise HTTPException(status_code=422, detail="会社名は必須です")

        res = supabase.table("companies").insert(payload).execute()
        logger.info(f"INSERT成功: {payload['name']}")
        return {"ok": True, "data": res.data[0] if res.data else None}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"company INSERT エラー: {e}")
        raise HTTPException(status_code=500, detail=f"追加失敗: {str(e)}")


# ============================================================
# 更新（UPDATE）
# ============================================================
@app.put("/company/{id}")
def update_company(id: int, data: CompanyUpdate):
    try:
        # Noneでないフィールドだけ更新
        payload = {k: v for k, v in data.model_dump().items() if v is not None}
        if not payload:
            raise HTTPException(status_code=422, detail="更新するフィールドがありません")

        res = supabase.table("companies").update(payload).eq("id", id).execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")

        logger.info(f"UPDATE成功: id={id}, payload={payload}")
        return {"ok": True, "data": res.data[0]}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"company UPDATE エラー (id={id}): {e}")
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")


# ============================================================
# 削除（DELETE）
# ============================================================
@app.delete("/company/{id}")
def delete_company(id: int):
    try:
        # 存在確認
        check = supabase.table("companies").select("id").eq("id", id).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")

        supabase.table("companies").delete().eq("id", id).execute()
        logger.info(f"DELETE成功: id={id}")
        return {"ok": True, "deleted_id": id}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"company DELETE エラー (id={id}): {e}")
        raise HTTPException(status_code=500, detail=f"削除失敗: {str(e)}")


# ============================================================
# ステータス変更（循環）
# ============================================================
@app.post("/status/{id}")
def change_status(id: int):
    try:
        # 現在のステータスを取得
        res = supabase.table("companies").select("status").eq("id", id).single().execute()

        if not res.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")

        current = res.data.get("status", "未対応")

        # 次のステータスを計算
        if current in STATUS_FLOW:
            next_status = STATUS_FLOW[(STATUS_FLOW.index(current) + 1) % len(STATUS_FLOW)]
        else:
            next_status = "未対応"

        # 更新
        supabase.table("companies").update({"status": next_status}).eq("id", id).execute()
        logger.info(f"STATUS変更: id={id}, {current} → {next_status}")

        return {
            "ok": True,
            "id": id,
            "before": current,
            "after": next_status,
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"status UPDATE エラー (id={id}): {e}")
        raise HTTPException(status_code=500, detail=f"ステータス更新失敗: {str(e)}")


# ============================================================
# ステータスを直接指定して変更
# ============================================================
@app.post("/status/{id}/set")
def set_status(id: int, status: str = Query(..., description="設定するステータス")):
    try:
        if status not in STATUS_FLOW:
            raise HTTPException(
                status_code=422,
                detail=f"無効なステータス。有効値: {STATUS_FLOW}"
            )

        check = supabase.table("companies").select("id").eq("id", id).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")

        supabase.table("companies").update({"status": status}).eq("id", id).execute()
        logger.info(f"STATUS直接設定: id={id} → {status}")

        return {"ok": True, "id": id, "status": status}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"status SET エラー (id={id}): {e}")
        raise HTTPException(status_code=500, detail=f"ステータス設定失敗: {str(e)}")
