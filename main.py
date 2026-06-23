import os
import logging
import httpx
from datetime import date
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional, List

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="営業リスト管理API", version="2.1.0")

# ============================================================
# CORS設定
# ============================================================
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ropehack.jp",
        "http://localhost:3000",
        "http://localhost:8080",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# ============================================================
# 環境変数
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY")

if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_URL または SUPABASE_KEY が設定されていません")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
logger.info(f"Supabase接続完了: {SUPABASE_URL}")

# ============================================================
# 定数
# ============================================================
STATUS_FLOW = ["未対応", "連絡済み", "商談中", "成約"]
DAILY_LIMIT = 30

# ★修正3: 対応エリアを関東地方のみに制限
KANTO_PREFECTURES = ["東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県"]

# Places API (New) エンドポイント
PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_DETAIL_URL = "https://places.googleapis.com/v1/places/{place_id}"

# ============================================================
# Pydanticモデル
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

class PlacesSearchResult(BaseModel):
    place_id: str
    name: str
    area: str
    phone: str
    url: str
    already_saved: bool  # 既にDBに存在するか

# ★修正1: ページネーション対応のため、検索結果は
#          「中身のリスト」と「次ページ取得用トークン」をまとめて返す
class PlacesSearchResponse(BaseModel):
    places: List[PlacesSearchResult]
    next_page_token: Optional[str] = None

# ============================================================
# ユーティリティ：今日の取得済み件数を返す
# ============================================================
def get_today_count() -> int:
    today = date.today().isoformat()
    try:
        res = supabase.table("daily_search_log") \
            .select("count") \
            .eq("search_date", today) \
            .execute()
        if res.data:
            return res.data[0]["count"]
        return 0
    except Exception as e:
        # ★修正2: ここが0のまま動かない最大の原因になりやすい箇所。
        #   daily_search_log テーブルが存在しない／RLSでブロックされていると
        #   例外が握りつぶされて常に0を返してしまう。
        #   schema.sql でテーブルを作成し、SUPABASE_KEYは
        #   service_role キーを使うこと（anonキーだとRLSで弾かれる）。
        logger.error(f"daily_search_log 取得エラー: {e}")
        return 0

# ============================================================
# ユーティリティ：今日のカウントを加算する
# ============================================================
def increment_today_count(amount: int) -> int:
    """加算後の最新カウントを返す（フロントの即時反映に使う）"""
    today = date.today().isoformat()
    try:
        existing = supabase.table("daily_search_log") \
            .select("count") \
            .eq("search_date", today) \
            .execute()

        if existing.data:
            new_count = existing.data[0]["count"] + amount
            supabase.table("daily_search_log") \
                .update({"count": new_count}) \
                .eq("search_date", today) \
                .execute()
        else:
            new_count = amount
            supabase.table("daily_search_log") \
                .insert({"search_date": today, "count": amount}) \
                .execute()
        return new_count
    except Exception as e:
        logger.error(f"daily_search_log 更新エラー: {e}")
        return get_today_count()

# ============================================================
# ユーティリティ：place_id が既にDBにあるか確認
# ============================================================
def is_already_saved(place_id: str) -> bool:
    try:
        res = supabase.table("companies") \
            .select("id") \
            .eq("place_id", place_id) \
            .execute()
        return len(res.data) > 0
    except Exception:
        return False

# ============================================================
# ヘルスチェック
# ============================================================
@app.get("/")
def health_check():
    return {"status": "ok", "message": "営業リスト管理API 稼働中"}

@app.get("/health")
def health():
    try:
        res = supabase.table("companies").select("id").limit(1).execute()
        return {"status": "ok", "db": "connected", "sample_count": len(res.data)}
    except Exception as e:
        logger.error(f"DB接続エラー: {e}")
        raise HTTPException(status_code=503, detail=f"DB接続失敗: {str(e)}")

# ============================================================
# Places API 検索（エリア×キーワード）
# ============================================================
@app.get("/places/search", response_model=PlacesSearchResponse)
async def search_places(
    area: str = Query(..., description="エリア（例：横浜）"),
    keyword: str = Query(..., description="キーワード（例：管理会社）"),
    max_results: int = Query(default=10, ge=1, le=20, description="1回の取得件数（最大20）"),
    page_token: Optional[str] = Query(
        default=None,
        description="前回レスポンスの next_page_token。指定すると「次の" "件」を取得する",
    ),
):
    """
    Google Places API (New) でエリア×キーワード検索を行い、
    会社情報のプレビューリストを返す。
    （この時点ではDBには保存しない）

    page_token を渡すと、前回と同じ検索条件のまま「次のページ」の
    結果を取得できる（＝同じ10件が返ってくる問題の対策）。
    """
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_PLACES_API_KEY が設定されていません")

    # ★修正3: 関東地方以外は弾く（フロントのセレクトも絞るが、念のためバックエンドでも検証）
    if not any(area.startswith(pref) for pref in KANTO_PREFECTURES):
        raise HTTPException(
            status_code=400,
            detail="対応エリアは関東地方（東京都・神奈川県・埼玉県・千葉県・茨城県・栃木県・群馬県）のみです",
        )

    # 1日の上限チェック
    today_count = get_today_count()
    if today_count >= DAILY_LIMIT:
        raise HTTPException(
            status_code=429,
            detail=f"本日の取得上限（{DAILY_LIMIT}件）に達しました。明日また試してください。"
        )

    # 残り取得可能件数を考慮して max_results を制限
    remaining = DAILY_LIMIT - today_count
    actual_max = min(max_results, remaining)

    query_text = f"{area} {keyword}"
    logger.info(
        f"Places検索: '{query_text}' (最大{actual_max}件, "
        f"page_token={'あり' if page_token else 'なし'}, 今日{today_count}/{DAILY_LIMIT}件済み)"
    )

    # Places API (New) テキスト検索リクエスト
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.websiteUri,"
            "places.regularOpeningHours,"
            "nextPageToken"  # ★修正1: 次ページトークンも取得する
        ),
    }

    # ★修正1: page_token がある場合は同じクエリ＋pageTokenで「次ページ」を取得。
    #   Google側は textQuery を省略するとエラーになることがあるため、
    #   pageToken と一緒に textQuery も送る（pageTokenが優先して使われる）。
    body = {
        "textQuery": query_text,
        "languageCode": "ja",
        "pageSize": actual_max,
    }
    if page_token:
        body["pageToken"] = page_token

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(PLACES_SEARCH_URL, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as e:
        logger.error(f"Places API HTTPエラー: {e.response.status_code} {e.response.text}")
        # pageTokenが無効/期限切れの場合もここに来る。フロントには分かるメッセージを返す。
        if page_token:
            raise HTTPException(
                status_code=502,
                detail="次ページの取得に失敗しました。少し時間を置いてもう一度検索してください。",
            )
        raise HTTPException(status_code=502, detail=f"Places API エラー: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Places API 通信エラー: {e}")
        raise HTTPException(status_code=502, detail=f"Places API 通信失敗: {str(e)}")

    places = data.get("places", [])
    next_token = data.get("nextPageToken")  # ★修正1: 無ければ None（=これ以上の結果なし）
    results: List[PlacesSearchResult] = []

    for p in places:
        place_id = p.get("id", "")
        name = p.get("displayName", {}).get("text", "")
        address = p.get("formattedAddress", "")
        phone = p.get("nationalPhoneNumber", "")
        website = p.get("websiteUri", "")

        # エリア抽出（住所から都道府県＋市区町村を取得）
        area_extracted = _extract_area(address, area)

        already = is_already_saved(place_id)

        results.append(PlacesSearchResult(
            place_id=place_id,
            name=name,
            area=area_extracted,
            phone=phone,
            url=website,
            already_saved=already,
        ))

    logger.info(f"Places検索結果: {len(results)}件取得 (next_page_token={'あり' if next_token else 'なし'})")
    return PlacesSearchResponse(places=results, next_page_token=next_token)


# ============================================================
# Places 検索結果を選択してDBに保存
# ============================================================
class SavePlacesRequest(BaseModel):
    places: List[dict]  # PlacesSearchResult の辞書リスト

@app.post("/places/save", status_code=201)
def save_places(req: SavePlacesRequest):
    """
    /places/search で取得した結果のうち、選択した会社をDBに一括保存する。
    ・重複（place_id が既存）はスキップ
    ・1日30件の上限を超える分はスキップ
    """
    today_count = get_today_count()
    remaining = DAILY_LIMIT - today_count

    if remaining <= 0:
        raise HTTPException(
            status_code=429,
            detail=f"本日の取得上限（{DAILY_LIMIT}件）に達しました。"
        )

    saved = []
    skipped_dup = []
    skipped_limit = []

    for item in req.places:
        if len(saved) >= remaining:
            skipped_limit.append(item.get("name", ""))
            continue

        place_id = item.get("place_id", "")

        # 重複チェック
        if place_id and is_already_saved(place_id):
            skipped_dup.append(item.get("name", ""))
            continue

        payload = {
            "name": item.get("name", "").strip(),
            "area": item.get("area", "").strip(),
            "email": "",   # Places API はメールを返さないため空
            "phone": item.get("phone", "").strip(),
            "url": item.get("url", "").strip(),
            "status": "未対応",
            "place_id": place_id,
        }

        if not payload["name"]:
            continue

        try:
            res = supabase.table("companies").insert(payload).execute()
            if res.data:
                saved.append(res.data[0])
        except Exception as e:
            logger.error(f"INSERT エラー ({payload['name']}): {e}")
            skipped_dup.append(payload["name"])

    # ★修正2: 加算後の最新カウントをその場で受け取り、レスポンスに含める。
    #   フロントはこの値をそのまま使えば、別途GETを叩いて競合（レース）する必要がない。
    if saved:
        today_new = increment_today_count(len(saved))
    else:
        today_new = today_count

    logger.info(f"保存完了: {len(saved)}件 / 重複スキップ: {len(skipped_dup)}件 / 上限スキップ: {len(skipped_limit)}件")

    return {
        "ok": True,
        "saved_count": len(saved),
        "skipped_duplicate": skipped_dup,
        "skipped_limit": skipped_limit,
        "today_total": today_new,
        "today_remaining": max(0, DAILY_LIMIT - today_new),
        "data": saved,
    }


# ============================================================
# 今日の取得状況を確認
# ============================================================
@app.get("/places/daily-status")
def daily_status():
    count = get_today_count()
    return {
        "date": date.today().isoformat(),
        "used": count,
        "remaining": max(0, DAILY_LIMIT - count),
        "limit": DAILY_LIMIT,
    }


# ============================================================
# エリア抽出ヘルパー
# ============================================================
def _extract_area(address: str, fallback: str) -> str:
    """
    住所文字列から都道府県＋市区町村を抽出する。
    例: "〒220-0011 神奈川県横浜市西区高島２丁目..." → "神奈川県横浜市"
    """
    import re
    # 都道府県パターン
    pref_pattern = r'(北海道|(?:東京|大阪|京都)府|.{2,3}県)'
    # 市区町村パターン
    city_pattern = r'(?:札幌市|(?:.+?)[市区町村])'

    pref_match = re.search(pref_pattern, address)
    if not pref_match:
        return fallback

    pref = pref_match.group(1)
    rest = address[pref_match.end():]
    city_match = re.match(city_pattern, rest)

    if city_match:
        return pref + city_match.group(0)
    return pref


# ============================================================
# 以下、既存のエンドポイント（変更なし）
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
            query = query.or_(
                f"name.ilike.%{keyword}%,"
                f"email.ilike.%{keyword}%,"
                f"phone.ilike.%{keyword}%"
            )

        if status:
            query = query.eq("status", status)

        query = query.order("id", desc=True)
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
            "place_id": "",
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


@app.put("/company/{id}")
def update_company(id: int, data: CompanyUpdate):
    try:
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


@app.delete("/company/{id}")
def delete_company(id: int):
    try:
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


@app.post("/status/{id}")
def change_status(id: int):
    try:
        res = supabase.table("companies").select("status").eq("id", id).single().execute()

        if not res.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")

        current = res.data.get("status", "未対応")

        if current in STATUS_FLOW:
            next_status = STATUS_FLOW[(STATUS_FLOW.index(current) + 1) % len(STATUS_FLOW)]
        else:
            next_status = "未対応"

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
