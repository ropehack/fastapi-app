from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

companies = []

@app.get("/companies")
def get_companies():
    return companies

@app.get("/search")
def search(area: str = "", keyword: str = ""):
    companies.append({
        "id": len(companies)+1,
        "name": keyword,
        "area": area
    })
    return {"ok": True}