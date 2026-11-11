import os
import json
import time
import uuid
import requests
from pathlib import Path
from typing import List, Optional
from fastapi import FastAPI, HTTPException, Header
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field
import yt_dlp
from google import genai
from google.genai import types

app = FastAPI(title="ReelChef AI")

TEMP_DIR = Path("temp_videos")
TEMP_DIR.mkdir(exist_ok=True)

# Variabili segrete che imposteremo su Render
APP_PIN = os.environ.get("APP_PIN", "1234")
SHEET_URL = os.environ.get("SHEET_URL", "")

client = genai.Client()

class Ingredient(BaseModel):
    item: str = Field(description="Nome dell'ingrediente, es. 'Guanciale' o 'Farina 00'")
    amount: str = Field(description="Quantità e unità di misura, es. '150g', '2 cucchiai', 'q.b.'")
    notes: Optional[str] = Field(default="", description="Dettagli extra")

class RecipeSchema(BaseModel):
    title: str = Field(description="Titolo del piatto")
    prep_time: str = Field(description="Tempo totale stimato (es. '25 min')")
    servings: int = Field(description="Numero di porzioni stimate")
    ingredients: List[Ingredient]
    steps: List[str] = Field(description="Passaggi chiari e dettagliati")
    chef_tips: Optional[str] = Field(default="", description="Trucchi visivi o note")

class ExtractRequest(BaseModel):
    url: str

class SubstituteRequest(BaseModel):
    recipe: dict
    modification_request: str

def check_pin(pin: Optional[str]):
    if pin != APP_PIN:
        raise HTTPException(status_code=401, detail="PIN errato! Inserisci il PIN di famiglia corretto.")

def save_to_sheet(recipe_data: dict):
    if SHEET_URL:
        try:
            requests.post(SHEET_URL, json={"action": "save", "recipe": recipe_data}, timeout=10)
        except Exception:
            pass

def download_facebook_reel(url: str) -> tuple[Path, str]:
    video_id = str(uuid.uuid4())[:8]
    output_template = str(TEMP_DIR / f"reel_{video_id}.%(ext)s")
    ydl_opts = {
        'format': 'worst[ext=mp4]/mp4/best',
        'outtmpl': output_template,
        'quiet': True,
        'no_warnings': True,
        'http_headers': {
            'User-Agent': 'Mozilla/5.0 (Linux; Android 14; Pixel 8a) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36'
        }
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        description = info.get('description') or info.get('title') or "Nessuna descrizione."
        downloaded_file = Path(ydl.prepare_filename(info))
    return downloaded_file, description

@app.post("/api/extract")
def extract_recipe(req: ExtractRequest, x_app_pin: Optional[str] = Header(None)):
    check_pin(x_app_pin)
    video_path = None
    uploaded_file = None
    try:
        video_path, caption = download_facebook_reel(req.url)
        uploaded_file = client.files.upload(file=str(video_path))
        
        while uploaded_file.state.name == "PROCESSING":
            time.sleep(2)
            uploaded_file = client.files.get(name=uploaded_file.name)
            
        if uploaded_file.state.name == "FAILED":
            raise Exception("Elaborazione video fallita sui server di Gemini.")

        prompt = f"""
        Sei uno chef professionista. Analizza attentamente questo Reel di cucina (video, testi in sovrimpressione e parlato).
        Descrizione del post: {caption}
        Estrai la ricetta completa in italiano. Se mancano dosi esatte, deducile tu in base alle proporzioni visibili specificando '(dose stimata)' nelle note.
        """

        response = client.models.generate_content(
            model='gemini-2.5-flash',
            contents=[uploaded_file, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RecipeSchema,
                temperature=0.2
            )
        )
        
        recipe_data = json.loads(response.text)
        recipe_data["id"] = str(uuid.uuid4())[:8]
        recipe_data["source_url"] = req.url
        
        save_to_sheet(recipe_data)
        return recipe_data

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        if video_path and video_path.exists():
            video_path.unlink()
        if uploaded_file:
            try:
                client.files.delete(name=uploaded_file.name)
            except Exception:
                pass

@app.post("/api/substitute")
def substitute_ingredients(req: SubstituteRequest, x_app_pin: Optional[str] = Header(None)):
    check_pin(x_app_pin)
    prompt = f"""
    Sei uno chef esperto. Ecco una ricetta in JSON:
    {json.dumps(req.recipe, ensure_ascii=False)}
    Richiesta di modifica: "{req.modification_request}"
    Riscrivi il JSON applicando la modifica, bilanciando le dosi e spiegando cosa cambia in 'chef_tips'.
    """
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=RecipeSchema,
            temperature=0.3
        )
    )
    updated_recipe = json.loads(response.text)
    updated_recipe["id"] = req.recipe.get("id", str(uuid.uuid4())[:8])
    updated_recipe["source_url"] = req.recipe.get("source_url", "")
    save_to_sheet(updated_recipe)
    return updated_recipe

@app.get("/api/recipes")
def get_saved_recipes(x_app_pin: Optional[str] = Header(None)):
    check_pin(x_app_pin)
    if not SHEET_URL:
        return []
    try:
        res = requests.get(SHEET_URL, timeout=10)
        return res.json()
    except Exception:
        return []

@app.delete("/api/recipes/{recipe_id}")
def delete_recipe(recipe_id: str, x_app_pin: Optional[str] = Header(None)):
    check_pin(x_app_pin)
    if SHEET_URL:
        try:
            requests.post(SHEET_URL, json={"action": "delete", "id": recipe_id}, timeout=10)
        except Exception:
            pass
    return {"status": "ok"}

@app.get("/manifest.json")
def get_manifest():
    return JSONResponse({
        "name": "ReelChef AI",
        "short_name": "ReelChef",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0f172a",
        "theme_color": "#ea580c",
        "icons": [{"src": "https://cdn-icons-png.flaticon.com/512/3565/3565418.png", "sizes": "512x512", "type": "image/png"}],
        "share_target": {
            "action": "/",
            "method": "GET",
            "enctype": "application/x-www-form-urlencoded",
            "params": {"title": "title", "text": "text", "url": "url"}
        }
    })

@app.get("/", response_class=HTMLResponse)
def serve_frontend():
    return Path("index.html").read_text(encoding="utf-8")
