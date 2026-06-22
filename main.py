"""
main.py（修正版）
-----------------
営業リスト管理SaaS - FastAPI
Supabase + RLS対応・エラーハンドリング強化版

エンドポイント:
  GET  /                 フロントエンド（index.html）
  GET  /api/health       ヘルスチェック
  GET  /api/companies    全企業取得
  GET  /api/search       企業検索（area/keyword フィルタ）
  POST /api/companies    企業追加
  PATCH /api/company/{id} ステータス更新
  DELETE /api/company/{id} 企業削除
  GET  /api/export/csv   CSV出力
"""

import csv
import io
import logging
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# === ローカルモジュール ===
# Render デプロイ時は supabase_client.py を同じディレクトリに配置
try:
    from supabase_client import (
        SupabaseError,
        check_duplicate_email,
        check_duplicate_url,
        delete_company,
        export_all_to_list,
        insert_company,
        select_all,
        select_with_filters,
        update_status,
    )
except ImportError as e:
    raise RuntimeError(f"supabase_client モジュールが見つかりません: {e}")

# ============================================================================
# ロギング設定
# ============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# ============================================================================
# FastAPI初期化
# ============================================================================
app = FastAPI(
    title="営業リスト管理SaaS",
    description="Supabase + FastAPI / RLS対応版",
    version="2.0.0",
)

# CORS設定（https://ropehack.jp からのリクエストを許可）
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://ropehack.jp",
        "http://localhost:3000",
        "http://localhost:5500",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)

# 静的ファイル配信（フロントエンド）
# Render デプロイ時は ./static フォルダに index.html を配置
try:
    app.mount("/static", StaticFiles(directory="static"), name="static")
except Exception as e:
    logger.warning(f"静的ファイルのマウントに失敗しました: {e}")

# ============================================================================
# ルート・ヘルスチェック
# ============================================================================

@app.get("/", include_in_schema=False)
def root():
    """フロントエンド（index.html）を返す"""
    try:
        return FileResponse("static/index.html")
    except Exception:
        return {"message": "Hello from Sales List SaaS API v2"}


@app.get("/api/health")
def health_check():
    """API稼働確認"""
    return {"status": "ok", "version": "2.0.0"}


# ============================================================================
# 企業データ取得API
# ============================================================================

@app.get("/api/companies")
def get_companies(
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    全企業データを取得（ページネーション対応）
    
    Parameters:
      - limit: 1回の取得件数（1-1000、デフォルト100）
      - offset: スキップ件数（デフォルト0）
    """
    try:
        logger.info(f"GET /api/companies: limit={limit}, offset={offset}")
        companies = select_all(limit=limit, offset=offset)
        logger.info(f"Found {len(companies)} companies")
        return {
            "success": True,
            "count": len(companies),
            "data": companies,
        }
    except SupabaseError as e:
        logger.error(f"Supabaseエラー: {e}")
        raise HTTPException(status_code=500, detail=f"データベースエラー: {str(e)}")
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        raise HTTPException(status_code=500, detail="予期しないエラーが発生しました")


@app.get("/api/search")
def search_companies(
    area: str = Query("", min_length=0),
    keyword: str = Query("", min_length=0),
    limit: int = Query(100, ge=1, le=1000),
    offset: int = Query(0, ge=0),
):
    """
    企業をエリア・キーワードで検索
    
    Parameters:
      - area: 検索エリア（例: 神奈川県）。空文字列は無視
      - keyword: 企業名キーワード（例: 管理会社）。空文字列は無視
      - limit: 1回の取得件数（1-1000、デフォルト100）
      - offset: スキップ件数（デフォルト0）
    
    Returns:
      - area・keyword の両方が空の場合は全データを返す
      - 片方が空の場合は その条件のみで検索
    """
    try:
        logger.info(f"GET /api/search: area={area!r}, keyword={keyword!r}, limit={limit}, offset={offset}")
        
        # 両方とも空の場合は全データ返却
        if not area.strip() and not keyword.strip():
            companies = select_all(limit=limit, offset=offset)
        else:
            companies = select_with_filters(area=area, keyword=keyword, limit=limit, offset=offset)
        
        logger.info(f"Search result: {len(companies)} companies")
        return {
            "success": True,
            "count": len(companies),
            "filters": {
                "area": area,
                "keyword": keyword,
            },
            "data": companies,
        }
    except SupabaseError as e:
        logger.error(f"Supabaseエラー: {e}")
        raise HTTPException(status_code=500, detail=f"データベースエラー: {str(e)}")
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        raise HTTPException(status_code=500, detail="予期しないエラーが発生しました")


# ============================================================================
# 企業データ作成API
# ============================================================================

@app.post("/api/companies")
def create_company(
    name: str = Query(..., min_length=1, max_length=255),
    type_: str = Query(..., alias="type", min_length=1, max_length=50),
    email: str = Query(..., min_length=1, max_length=255),
    area: str = Query(..., min_length=1, max_length=100),
    status: str = Query("未送信", regex="^(未送信|営業済み)$"),
    url: Optional[str] = Query(None, max_length=500),
):
    """
    新しい企業データを追加
    
    重複チェック:
      - email: メールアドレスが既に存在しないか確認
      - url: URLが既に存在しないか確認
    """
    try:
        logger.info(f"POST /api/companies: name={name}, email={email}")
        
        # 重複チェック
        if check_duplicate_email(email):
            logger.warning(f"重複: email={email}")
            raise HTTPException(
                status_code=409,
                detail=f"このメールアドレスは既に登録されています: {email}"
            )
        
        if url and check_duplicate_url(url):
            logger.warning(f"重複: url={url}")
            raise HTTPException(
                status_code=409,
                detail=f"このURLは既に登録されています: {url}"
            )
        
        # 挿入
        company = insert_company(
            name=name,
            type_=type_,
            email=email,
            area=area,
            status=status,
            url=url,
        )
        logger.info(f"企業を追加しました: id={company['id']}")
        return {
            "success": True,
            "data": company,
        }
    except HTTPException:
        raise
    except SupabaseError as e:
        logger.error(f"Supabaseエラー: {e}")
        raise HTTPException(status_code=500, detail=f"データベースエラー: {str(e)}")
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        raise HTTPException(status_code=500, detail="予期しないエラーが発生しました")


# ============================================================================
# 企業データ更新API
# ============================================================================

@app.patch("/api/company/{company_id}")
def update_company_status(
    company_id: int = Query(..., ge=1),
    status: str = Query(..., regex="^(未送信|営業済み)$"),
):
    """
    企業のステータスを更新
    
    Parameters:
      - company_id: 企業ID
      - status: 新しいステータス（未送信 or 営業済み）
    """
    try:
        logger.info(f"PATCH /api/company/{company_id}: status={status}")
        company = update_status(company_id=company_id, status=status)
        logger.info(f"ステータスを更新しました: id={company_id}, status={status}")
        return {
            "success": True,
            "data": company,
        }
    except SupabaseError as e:
        logger.error(f"Supabaseエラー: {e}")
        if "見つかりません" in str(e):
            raise HTTPException(status_code=404, detail="企業が見つかりません")
        raise HTTPException(status_code=500, detail=f"データベースエラー: {str(e)}")
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        raise HTTPException(status_code=500, detail="予期しないエラーが発生しました")


# ============================================================================
# 企業データ削除API
# ============================================================================

@app.delete("/api/company/{company_id}")
def delete_company_by_id(company_id: int = Query(..., ge=1)):
    """
    企業データを削除
    
    Parameters:
      - company_id: 企業ID
    """
    try:
        logger.info(f"DELETE /api/company/{company_id}")
        delete_company(company_id=company_id)
        logger.info(f"企業を削除しました: id={company_id}")
        return {
            "success": True,
            "deleted_id": company_id,
        }
    except SupabaseError as e:
        logger.error(f"Supabaseエラー: {e}")
        raise HTTPException(status_code=500, detail=f"データベースエラー: {str(e)}")
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        raise HTTPException(status_code=500, detail="予期しないエラーが発生しました")


# ============================================================================
# CSV エクスポートAPI
# ============================================================================

@app.get("/api/export/csv")
def export_csv():
    """
    全企業データをCSV形式でエクスポート
    
    ファイル形式: UTF-8 BOM付き（Excel対応）
    列: 会社名, 種別, メールアドレス, エリア, ステータス, URL
    """
    try:
        logger.info("GET /api/export/csv")
        companies = export_all_to_list()
        logger.info(f"Exporting {len(companies)} companies to CSV")
        
        # CSV作成
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["会社名", "種別", "メールアドレス", "エリア", "ステータス", "URL"])
        
        for company in companies:
            writer.writerow([
                company.get("name", ""),
                company.get("type", ""),
                company.get("email", ""),
                company.get("area", ""),
                company.get("status", ""),
                company.get("url", ""),
            ])
        
        # BOM付きUTF-8で返す（Excelで文字化けしない）
        csv_bytes = ("\ufeff" + buffer.getvalue()).encode("utf-8")
        
        return StreamingResponse(
            io.BytesIO(csv_bytes),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=companies.csv"},
        )
    except SupabaseError as e:
        logger.error(f"Supabaseエラー: {e}")
        raise HTTPException(status_code=500, detail=f"データベースエラー: {str(e)}")
    except Exception as e:
        logger.exception(f"予期しないエラー: {e}")
        raise HTTPException(status_code=500, detail="予期しないエラーが発生しました")


# ============================================================================
# エラーハンドラ
# ============================================================================

@app.exception_handler(Exception)
async def global_exception_handler(request, exc):
    """全体エラーハンドラ"""
    logger.exception(f"Unhandled exception: {exc}")
    return {
        "success": False,
        "error": "An unexpected error occurred",
        "detail": str(exc),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
