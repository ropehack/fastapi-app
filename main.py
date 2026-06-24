import os
import re
import logging
import httpx
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from supabase import create_client, Client
from pydantic import BaseModel
from typing import Optional, List
from bs4 import BeautifulSoup

# ============================================================
# タイムゾーン：JST固定（RenderサーバーはUTC）
# ============================================================
JST = timezone(timedelta(hours=9))

def today_jst() -> str:
    """日本時間の今日の日付を YYYY-MM-DD 文字列で返す"""
    return datetime.now(JST).strftime("%Y-%m-%d")

# ============================================================
# ログ設定
# ============================================================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="営業リスト管理API", version="2.4.0")

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
STATUS_FLOW = ["未送信", "送信済み", "商談中"]
DAILY_LIMIT = 30

KANTO_PREFECTURES = ["東京都", "神奈川県", "埼玉県", "千葉県", "茨城県", "栃木県", "群馬県"]

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
    already_saved: bool

class PlacesSearchResponse(BaseModel):
    places: List[PlacesSearchResult]
    next_page_token: Optional[str] = None

# ============================================================
# ユーティリティ：今日の取得済み件数
# ============================================================
def get_today_count() -> int:
    today = today_jst()
    try:
        res = supabase.table("daily_search_log") \
            .select("count") \
            .eq("search_date", today) \
            .execute()
        if res.data:
            return res.data[0]["count"]
        return 0
    except Exception as e:
        logger.error(f"daily_search_log 取得エラー: {e}")
        return 0

def increment_today_count(amount: int) -> int:
    today = today_jst()
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
# IDを1から振り直すヘルパー
# ============================================================
def resequence_ids() -> int:
    try:
        res = supabase.table("companies").select("id").order("id", desc=False).execute()
        rows = res.data or []

        if not rows:
            return 0

        for row in rows:
            supabase.table("companies") \
                .update({"id": row["id"] + 100000}) \
                .eq("id", row["id"]) \
                .execute()

        for new_id, row in enumerate(rows, start=1):
            supabase.table("companies") \
                .update({"id": new_id}) \
                .eq("id", row["id"] + 100000) \
                .execute()

        max_id = len(rows)

        try:
            supabase.rpc("setval_companies_id_seq", {"new_val": max_id}).execute()
            logger.info(f"シーケンスリセット完了: {max_id}")
        except Exception as rpc_err:
            logger.warning(f"シーケンスRPCスキップ: {rpc_err}")

        logger.info(f"ID振り直し完了: {max_id}件")
        return max_id
    except Exception as e:
        logger.error(f"ID振り直しエラー: {e}")
        raise

# ============================================================
# メール／フォームURL スクレイピング
# ============================================================
SCRAPE_TIMEOUT = 8.0
SCRAPE_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; SalesBot/1.0)"}
CONTACT_KEYWORDS = ["お問い合わせ", "問合せ", "問い合わせ", "contact", "Contact", "CONTACT", "inquiry", "Inquiry"]
EMAIL_REGEX = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z]{2,}")
INVALID_EMAIL_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")

async def scrape_contact_from_url(url: str) -> str:
    """
    指定URLとコンタクトページ候補を巡回し、
    メールアドレス → フォームURL の優先順で連絡先を返す。
    何も取れなければ空文字を返す。
    """
    if not url:
        return ""

    base = url.rstrip("/")
    # トップページ → コンタクト候補ページの順に試す
    candidates = [url] + [
        base + path
        for path in ["/contact", "/contact-us", "/お問い合わせ", "/inquiry", "/form"]
    ]

    form_url_found = ""  # フォームURLの候補（メールが見つからない場合の代替）

    async with httpx.AsyncClient(
        timeout=SCRAPE_TIMEOUT,
        follow_redirects=True,
        headers=SCRAPE_HEADERS,
    ) as client:
        for target_url in candidates:
            try:
                resp = await client.get(target_url)
                if resp.status_code != 200:
                    continue

                soup = BeautifulSoup(resp.text, "html.parser")

                # ① mailto: リンクを最優先
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    if href.startswith("mailto:"):
                        email = href.replace("mailto:", "").split("?")[0].strip()
                        if email and "@" in email and not email.endswith(INVALID_EMAIL_EXTS):
                            logger.info(f"メール取得(mailto): {email} from {target_url}")
                            return email

                # ② テキストからメール正規表現
                text = soup.get_text(" ", strip=True)
                emails = EMAIL_REGEX.findall(text)
                filtered = [
                    e for e in emails
                    if not e.endswith(INVALID_EMAIL_EXTS)
                ]
                if filtered:
                    logger.info(f"メール取得(regex): {filtered[0]} from {target_url}")
                    return filtered[0]

                # ③ フォームURLを記憶（まだ見つかっていない場合のみ）
                if not form_url_found:
                    # <form> タグがあるページはそのURLをフォームとして記録
                    if soup.find("form"):
                        form_url_found = str(resp.url)
                        logger.info(f"フォームURL候補: {form_url_found}")

                    # 「お問い合わせ」リンクを探してフォームURLとして記録
                    if not form_url_found:
                        for a in soup.find_all("a", href=True):
                            link_text = a.get_text(strip=True)
                            if any(kw in link_text for kw in CONTACT_KEYWORDS):
                                href = a["href"]
                                if href.startswith("http"):
                                    form_url_found = href
                                elif href.startswith("/"):
                                    form_url_found = base + href
                                if form_url_found:
                                    logger.info(f"フォームURL候補(リンク): {form_url_found}")
                                    break

            except Exception as e:
                logger.debug(f"スクレイピングスキップ ({target_url}): {e}")
                continue

    # メールが取れなかった場合はフォームURLを返す
    return form_url_found

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
# daily-status
# ============================================================
@app.get("/places/daily-status")
def daily_status():
    count = get_today_count()
    logger.info(f"daily-status: used={count}, remaining={max(0, DAILY_LIMIT - count)}")
    return {
        "date": today_jst(),
        "used": count,
        "remaining": max(0, DAILY_LIMIT - count),
        "limit": DAILY_LIMIT,
    }

# ============================================================
# Places API 検索
# ============================================================
@app.get("/places/search", response_model=PlacesSearchResponse)
async def search_places(
    area: str = Query(...),
    keyword: str = Query(...),
    max_results: int = Query(default=10, ge=1, le=20),
    page_token: Optional[str] = Query(default=None),
):
    if not GOOGLE_PLACES_API_KEY:
        raise HTTPException(status_code=500, detail="GOOGLE_PLACES_API_KEY が設定されていません")

    if not any(area.startswith(pref) for pref in KANTO_PREFECTURES):
        raise HTTPException(status_code=400, detail="対応エリアは関東地方のみです")

    today_count = get_today_count()
    if today_count >= DAILY_LIMIT:
        raise HTTPException(status_code=429, detail=f"本日の取得上限（{DAILY_LIMIT}件）に達しました。")

    remaining = DAILY_LIMIT - today_count
    actual_max = min(max_results, remaining)

    query_text = f"{area} {keyword}"
    logger.info(f"Places検索: '{query_text}' (最大{actual_max}件, 今日{today_count}/{DAILY_LIMIT}件済み)")

    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": (
            "places.id,"
            "places.displayName,"
            "places.formattedAddress,"
            "places.nationalPhoneNumber,"
            "places.websiteUri,"
            "nextPageToken"
        ),
    }

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
        raise HTTPException(status_code=502, detail=f"Places API エラー: {e.response.status_code}")
    except Exception as e:
        logger.error(f"Places API 通信エラー: {e}")
        raise HTTPException(status_code=502, detail=f"Places API 通信失敗: {str(e)}")

    places = data.get("places", [])
    next_token = data.get("nextPageToken")
    results: List[PlacesSearchResult] = []

    for p in places:
        place_id = p.get("id", "")
        name = p.get("displayName", {}).get("text", "")
        address = p.get("formattedAddress", "")
        phone = p.get("nationalPhoneNumber", "")
        website = p.get("websiteUri", "")
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

    logger.info(f"Places検索結果: {len(results)}件取得")
    return PlacesSearchResponse(places=results, next_page_token=next_token)

# ============================================================
# Places 検索結果をDBに保存（スクレイピング付き）
# ============================================================
class SavePlacesRequest(BaseModel):
    places: List[dict]

@app.post("/places/save", status_code=201)
async def save_places(req: SavePlacesRequest):  # async に変更
    today_count = get_today_count()
    remaining = DAILY_LIMIT - today_count

    if remaining <= 0:
        raise HTTPException(status_code=429, detail=f"本日の取得上限（{DAILY_LIMIT}件）に達しました。")

    saved = []
    skipped_dup = []
    skipped_limit = []

    for item in req.places:
        if len(saved) >= remaining:
            skipped_limit.append(item.get("name", ""))
            continue

        place_id = item.get("place_id", "")
        if place_id and is_already_saved(place_id):
            skipped_dup.append(item.get("name", ""))
            continue

        url = item.get("url", "").strip()

        # URLがあればメール or フォームURLをスクレイピング
        email = ""
        if url:
            try:
                email = await scrape_contact_from_url(url)
            except Exception as e:
                logger.warning(f"スクレイピング失敗 ({url}): {e}")

        payload = {
            "name": item.get("name", "").strip(),
            "area": item.get("area", "").strip(),
            "email": email,          # メール or フォームURL or ""
            "phone": item.get("phone", "").strip(),
            "url": url,
            "status": "未送信",
            "place_id": place_id,
        }

        if not payload["name"]:
            continue

        try:
            res = supabase.table("companies").insert(payload).execute()
            if res.data:
                saved.append(res.data[0])
                logger.info(
                    f"保存: {payload['name']} / email={email or '(なし)'}"
                )
        except Exception as e:
            logger.error(f"INSERT エラー ({payload['name']}): {e}")
            skipped_dup.append(payload["name"])

    if saved:
        today_new = increment_today_count(len(saved))
    else:
        today_new = today_count

    logger.info(
        f"保存完了: {len(saved)}件 / 重複: {len(skipped_dup)}件 / "
        f"上限超過: {len(skipped_limit)}件 / 本日合計: {today_new}"
    )

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
# エリア抽出ヘルパー
# ============================================================
def _extract_area(address: str, fallback: str) -> str:
    pref_pattern = r'(北海道|(?:東京|大阪|京都)府|.{2,3}県)'
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
# 会社一覧取得
# ============================================================
@app.get("/companies")
def get_companies(
    area: str = Query(default=""),
    keyword: str = Query(default=""),
    status: str = Query(default=""),
    limit: int = Query(default=10, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
):
    try:
        query = supabase.table("companies").select("*", count="exact")
        if area:
            query = query.eq("area", area)
        if keyword:
            query = query.or_(f"name.ilike.%{keyword}%,email.ilike.%{keyword}%,phone.ilike.%{keyword}%")
        if status:
            query = query.eq("status", status)
        query = query.order("id", desc=False)
        res = query.range(offset, offset + limit - 1).execute()
        return {"data": res.data, "total": res.count, "limit": limit, "offset": offset}
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
            "status": "未送信",
            "place_id": "",
        }
        if not payload["name"]:
            raise HTTPException(status_code=422, detail="会社名は必須です")
        res = supabase.table("companies").insert(payload).execute()
        return {"ok": True, "data": res.data[0] if res.data else None}
    except HTTPException:
        raise
    except Exception as e:
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
        return {"ok": True, "data": res.data[0]}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"更新失敗: {str(e)}")

# ============================================================
# 削除 → ID振り直し
# ============================================================
@app.delete("/company/{id}")
def delete_company(id: int):
    try:
        check = supabase.table("companies").select("id").eq("id", id).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")

        supabase.table("companies").delete().eq("id", id).execute()
        logger.info(f"DELETE成功: id={id}")

        new_total = resequence_ids()
        return {"ok": True, "deleted_id": id, "new_total": new_total}

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"company DELETE エラー (id={id}): {e}")
        raise HTTPException(status_code=500, detail=f"削除失敗: {str(e)}")

# ============================================================
# ステータス変更
# ============================================================
@app.post("/status/{id}")
def change_status(id: int):
    try:
        res = supabase.table("companies").select("status").eq("id", id).single().execute()
        if not res.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")
        current = res.data.get("status", "未送信")
        if current in STATUS_FLOW:
            next_status = STATUS_FLOW[(STATUS_FLOW.index(current) + 1) % len(STATUS_FLOW)]
        else:
            next_status = "未送信"
        supabase.table("companies").update({"status": next_status}).eq("id", id).execute()
        return {"ok": True, "id": id, "before": current, "after": next_status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ステータス更新失敗: {str(e)}")

@app.post("/status/{id}/set")
def set_status(id: int, status: str = Query(...)):
    try:
        if status not in STATUS_FLOW:
            raise HTTPException(status_code=422, detail=f"無効なステータス。有効値: {STATUS_FLOW}")
        check = supabase.table("companies").select("id").eq("id", id).execute()
        if not check.data:
            raise HTTPException(status_code=404, detail="会社が見つかりません")
        supabase.table("companies").update({"status": status}).eq("id", id).execute()
        return {"ok": True, "id": id, "status": status}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"ステータス設定失敗: {str(e)}")
