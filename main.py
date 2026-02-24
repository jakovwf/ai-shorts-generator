import os
import time
import json
import logging
import re
from datetime import datetime
from io import BytesIO
from PIL import Image
from google import genai
from google.genai import types
from pydantic import BaseModel, Field
from dataclasses import dataclass
from dotenv import load_dotenv
from typing import Optional, List
from moviepy import VideoFileClip, concatenate_videoclips, vfx, TextClip, CompositeVideoClip

# ---------------------------------------------------------
# UČITAVANJE .ENV
# ---------------------------------------------------------
load_dotenv(override=True)

@dataclass
class Config:
    API_KEY: str = os.getenv("GOOGLE_API_KEY")
    
    TEXT_MODEL: str = "gemini-3-flash-preview"
    IMAGE_MODEL: str = "gemini-3-pro-image-preview" 
    VIDEO_MODEL: str = "veo-3.1-generate-preview" 
    
    IMAGE_RES: str = "2K" 
    IMAGE_ASPECT: str = "9:16"
    VIDEO_RES: str = "720p" 
    VIDEO_ASPECT: str = "9:16"
    
    BASE_OUTPUT_FOLDER: str = "Luxury_Architectural_Studio"
    IDEAS_FILE: str = "luxury_ideas.txt"
    COMPLETED_IDEAS_FILE: str = "completed_ideas.txt" 
    STRATEGY_FILE: str = "strategy_update.json" # 🔥 Closed-loop feedback fajl
    
    MAX_API_RETRIES: int = 3
    POLLING_INTERVAL: int = 10
    API_CALL_DELAY: float = 2.0
    INTER_VIDEO_DELAY: int = 15
    
    # 🔥 MANUAL MODE FLAG: Generiše samo slike i metadata (štedi Veo limit)
    SKIP_VIDEO_GENERATION: bool = True

config = Config()

# ---------------------------------------------------------
# LOGGING
# ---------------------------------------------------------
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

try:
    client = genai.Client(api_key=config.API_KEY)
except Exception as e:
    logger.error(f"Greska pri inicijalizaciji klijenta: {e}")
    exit(1)

# ---------------------------------------------------------
# STRUKTURE PODATAKA
# ---------------------------------------------------------
class ViralIdeaFilter(BaseModel):
    viral_ideas: List[str] = Field(description="List of ideas that scored 8/10 or higher for virality.")

class ViralVideoScript(BaseModel):
    title_hook: str = Field(description="Elegant, authoritative title")
    hook_text_overlay: str = Field(description="Short, highly engaging text for the first 2 seconds on screen")
    description: str = Field(description="Professional description")
    pinned_comment_cta: str = Field(description="Engaging pinned comment asking a question and including an Affiliate Link placeholder '[LINK]'")
    hashtags_youtube: str = Field(description="Space-separated optimized hashtags for YouTube Shorts")
    hashtags_tiktok: str = Field(description="Space-separated optimized hashtags for TikTok")
    hashtags_reels: str = Field(description="Space-separated optimized hashtags for IG Reels")
    image_prompts: List[str] = Field(description="Exactly 4 image prompts: 1. Existing Condition, 2. Structural Work, 3. Refinement, 4. Completed Result")
    video_prompts: List[str] = Field(description="Exactly 4 video prompts focusing on progression using '->'")

# ---------------------------------------------------------
# SISTEMSKI PROMPTOVI (SA STRATEGY LOADER-OM)
# ---------------------------------------------------------
def get_ideation_prompt():
    """Učitava strategiju iz analitike i dinamički menja prompt za ideje."""
    base_prompt = """You are a luxury real estate developer and interior design strategist in Los Angeles and New York.
Generate 5 high-end, satisfying architectural renovation concepts that appeal to Western audiences (US/EU).
Focus on satisfying DIY, modern epoxy pours, 100-year-old floor restorations, or ugly garage to luxury showroom transformations.
Format: One idea per line."""
    
    if os.path.exists(config.STRATEGY_FILE):
        try:
            with open(config.STRATEGY_FILE, "r", encoding="utf-8") as f:
                strategy = json.load(f)
                pri = ", ".join(strategy.get("priority_keywords", []))
                av = ", ".join(strategy.get("avoid_keywords", []))
                
                if pri or av:
                    base_prompt += "\n\n=== 📈 VIRAL ANALYTICS DATA (STRICT ADHERENCE REQUIRED) ===\n"
                    if pri: base_prompt += f"- STRONGLY PRIORITIZE THESE CONCEPTS: {pri}\n"
                    if av: base_prompt += f"- STRICTLY AVOID THESE CONCEPTS: {av}\n"
        except Exception as e:
            logger.warning(f"⚠️ Ne mogu da učitam analitiku ({config.STRATEGY_FILE}): {e}")
            
    return base_prompt

SCRIPTING_SYSTEM_PROMPT = """
You are a luxury architectural build director and viral marketing expert.
Create a premium viral time-lapse script.

VISUAL IDENTITY: Clean framing, realistic tools, highly satisfying ASMR movement.

VIDEO STRUCTURE (4 REQUIRED - ALL FOCUSED ON PROGRESSION):
Clip 1 – The Fast Setup: raw space -> materials prepped and leveled.
Clip 2 – The Hyperlapse Build: prepped area -> main structures rapidly built or poured.
Clip 3 – The High-Speed Details: raw structures -> surfaces smoothed, sanded, and finished.
Clip 4 – The Loop Transition: completed luxury build -> original raw condition restored in seamless reverse time-lapse.

CRITICAL RULE:
Every video prompt MUST explicitly use the "->" symbol.
Include an engaging Hook Text Overlay and a Pinned Comment for an affiliate product related to the build.
"""

# ---------------------------------------------------------
# POMOĆNE FUNKCIJE
# ---------------------------------------------------------
def pil_to_bytes(pil_img, mime_type="image/png") -> bytes:
    buf = BytesIO()
    pil_img.save(buf, format="PNG")
    return buf.getvalue()

def sanitize_filename(name: str) -> str:
    return re.sub(r'[^\w\s-]', '', name).strip().replace(' ', '_')[:30]

def validate_progression(prompt: str) -> bool:
    # 🛠️ POPRAVKA: Striktnija validacija (mora imati strelicu)
    return prompt.count("->") >= 1

def save_metadata(project_path, script):
    """Snima kompletan YouTube paket ODMAH nakon slika."""
    meta_path = os.path.join(project_path, "youtube_metadata.txt")
    with open(meta_path, "w", encoding="utf-8") as f:
        f.write(f"=== METADATA ZA COPY/PASTE ===\n\nTITLE: {script.title_hook}\n\nDESCRIPTION:\n{script.description}\n\n")
        f.write(f"--- MONETIZACIJA & MARKETING ---\n📌 PINNED COMMENT (Affiliate):\n{script.pinned_comment_cta}\n\n")
        f.write(f"📺 YT HASHTAGS: {script.hashtags_youtube}\n📱 TIKTOK HASHTAGS: {script.hashtags_tiktok}\n📸 REELS HASHTAGS: {script.hashtags_reels}\n")
    logger.info("📄 YouTube metadata fajl uspešno osiguran.")

# ---------------------------------------------------------
# GENERATORI (SA AI FILTEROM)
# ---------------------------------------------------------
def generate_ideas():
    logger.info("💡 1/2 Generišem sirove arhitektonske koncepte (Uzimam Analitiku u obzir)...")
    try:
        response1 = client.models.generate_content(
            model=config.TEXT_MODEL,
            contents=get_ideation_prompt(), # 🔥 Poziva dinamički sistem
            config=types.GenerateContentConfig(thinking_config=types.ThinkingConfig(thinking_level="low"))
        )
        
        logger.info("🤖 2/2 AI Evaluator ocenjuje ideje (Ostavlja samo > 8/10)...")
        evaluator_prompt = f"Evaluate these video ideas for US/EU YouTube Shorts. Score them from 1 to 10 for virality and retention. Return ONLY the ones that score 8 or higher.\n\nIDEAS:\n{response1.text}"
        
        response2 = client.models.generate_content(
            model=config.TEXT_MODEL, contents=evaluator_prompt,
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ViralIdeaFilter)
        )
        
        filtered_ideas = response2.parsed.viral_ideas
        if filtered_ideas:
            with open(config.IDEAS_FILE, "a", encoding="utf-8") as f:
                for idea in filtered_ideas: f.write(idea.strip() + "\n")
            logger.info(f"✅ Prošlo filter: {len(filtered_ideas)} ultra-viralnih ideja.")
            return True
        else:
            logger.warning("⚠️ Nijedna ideja nije prešla ocenu 8. Pokušaću ponovo kasnije.")
            return False
    except Exception as e:
        logger.error(f"❌ Greška pri ideaciji/filtriranju: {e}")
        return False

def generate_script(idea: str) -> Optional[ViralVideoScript]:
    logger.info(f"🧠 (Thinking) Pišem viralnu skriptu i CTA za: {idea}")
    try:
        response = client.models.generate_content(
            model=config.TEXT_MODEL, contents=f"{SCRIPTING_SYSTEM_PROMPT}\n\nTOPIC: {idea}",
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=ViralVideoScript, thinking_config=types.ThinkingConfig(thinking_level="high"))
        )
        return response.parsed 
    except Exception as e:
        logger.error(f"❌ Greška skripta: {e}")
        return None

def generate_images(script: ViralVideoScript, save_dir: str):
    logger.info("🎨 Renderujem Architectural Digest slike...")
    images = []
    chat = client.chats.create(
        model=config.IMAGE_MODEL,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"], image_config=types.ImageConfig(aspect_ratio=config.IMAGE_ASPECT, image_size=config.IMAGE_RES))
    )

    for i, prompt in enumerate(script.image_prompts):
        logger.info(f"    🖌️ Slika {i+1}/4...")
        img_saved = False
        for attempt in range(config.MAX_API_RETRIES): 
            try:
                full_prompt = prompt if i == 0 else prompt + " Maintain EXACT camera angle, lens focal length and perspective as the previous image."
                response = chat.send_message(full_prompt)
                
                if not response.parts: continue

                for part in response.parts:
                    if hasattr(part, 'inline_data') and part.inline_data:
                        img = Image.open(BytesIO(part.inline_data.data))
                        path = os.path.join(save_dir, f"img_{i+1}.png")
                        img.save(path)
                        images.append(img)
                        img_saved = True
                        time.sleep(config.API_CALL_DELAY) 
                        break
                if img_saved: break
            except Exception as e:
                logger.warning(f"⚠️ Greška slika {i+1} (pokušaj {attempt+1}): {e}")
                time.sleep(5)
        
        if not img_saved: return None
    return images

def generate_video_clip(start_img, end_img, prompt, save_path):
    logger.info(f"🎥 (Veo 3.1) Render: {prompt[:30]}...")
    start_bytes, end_bytes = pil_to_bytes(start_img), pil_to_bytes(end_img)
    img_input = types.Image(image_bytes=start_bytes, mime_type="image/png")
    last_frame_input = types.Image(image_bytes=end_bytes, mime_type="image/png")

    full_prompt = f"Construction time-lapse: {prompt}\nLocked tripod shot. Fast architectural time-lapse. Clear visible transformation. Workers moving rapidly. Stable geometry."

    for attempt in range(config.MAX_API_RETRIES):
        try:
            op = client.models.generate_videos(
                model=config.VIDEO_MODEL, prompt=full_prompt, image=img_input,  
                config=types.GenerateVideosConfig(last_frame=last_frame_input, aspect_ratio=config.VIDEO_ASPECT, resolution=config.VIDEO_RES, negative_prompt="cartoon, cgi, blur, morphing")
            )
            while not op.done:
                time.sleep(config.POLLING_INTERVAL)
                op = client.operations.get(op)
                
            if op.response.generated_videos:
                video = op.response.generated_videos[0].video
                client.files.download(file=video)
                video.save(save_path)
                logger.info(f"✅ Video sačuvan: {save_path}")
                return True
            else:
                logger.warning(f"⚠️ API nije vratio video (Pokušaj {attempt+1})")
        except Exception as e:
            logger.error(f"❌ Greška Veo video (Pokušaj {attempt+1}): {e}")
            time.sleep(10) 
    return False

# ---------------------------------------------------------
# MONTAŽA (SA DURATION AUTOSCALING-OM I HOOK-OM)
# ---------------------------------------------------------
def edit_video(project_path, script):
    logger.info("🎬 LUXURY VIRAL EDIT (Tražim analitiku za optimizaciju dužine)...")
    clips = []
    final = None
    loop_tail = None

    try:
        # 🔥 AUTOSCALING BRZINE NA OSNOVU ANALITIKE
        target_duration = None
        if os.path.exists(config.STRATEGY_FILE):
            with open(config.STRATEGY_FILE, "r", encoding="utf-8") as f:
                strategy = json.load(f)
                if "optimal_duration_range_seconds" in strategy:
                    rng = strategy["optimal_duration_range_seconds"]
                    target_duration = (rng[0] + rng[1]) / 2.0
                    target_duration = max(10.0, min(30.0, target_duration)) # Bezbednosni okvir
        
        speed_scale = 1.0
        if target_duration:
            # Procenjeno podrazumevano trajanje sa base brzinama je ~14.4 sekundi
            speed_scale = 14.4 / target_duration
            logger.info(f"📈 Analitika kaže: Meta trajanje {target_duration}s. Skaliram brzine x{speed_scale:.2f}")

        base_speeds = [2.5, 1.5, 2.0, 1.2]

        for i in range(1, 5):
            p = os.path.join(project_path, "videi", f"clip_{i}.mp4")
            if os.path.exists(p):
                clip = VideoFileClip(p)
                source_duration = min(clip.duration, 6.0)
                
                # Primena prilagođene brzine (sprečavamo preveliko usporavanje ispod 0.5x)
                final_speed = max(0.5, base_speeds[i-1] * speed_scale)
                clip = clip.subclipped(0, source_duration).with_effects([vfx.MultiplySpeed(final_speed)])

                if i == 1:
                    try:
                        txt_duration = min(2.5, clip.duration)
                        txt_clip = TextClip(script.hook_text_overlay, fontsize=60, color='white', font='Arial-Bold', stroke_color='black', stroke_width=2)
                        txt_clip = txt_clip.set_position('center').set_duration(txt_duration)
                        clip = CompositeVideoClip([clip, txt_clip])
                    except Exception as e:
                        logger.warning(f"⚠️ Text Overlay preskočen: {e}")
                        
                clips.append(clip)

        if clips:
            final = concatenate_videoclips(clips, method="compose")
            
            try:
                tail_duration = min(0.2, clips[0].duration) 
                loop_tail = clips[0].subclipped(0, tail_duration).with_effects([vfx.FadeIn(0.1)])
                final = concatenate_videoclips([final, loop_tail], method="compose")
            except Exception as e:
                logger.warning(f"⚠️ Loop tail greška: {e}")
            
            out = os.path.join(project_path, "FINAL_LUXURY_VIRAL.mp4")
            final.write_videofile(out, codec="libx264", audio_codec="aac", fps=30, logger=None)
            logger.info(f"🏆 VIRAL VIDEO SPREMAN: {out} (Trajanje: {final.duration:.1f}s)")
            return True
            
    except Exception as e:
        logger.error(f"EDIT ERROR: {e}")
        return False
        
    finally:
        if final is not None:
            try: final.close()
            except: pass
        if loop_tail is not None:
            try: loop_tail.close()
            except: pass
        for c in clips:
            try: c.close()
            except: pass

# ---------------------------------------------------------
# GLAVNA PETLJA
# ---------------------------------------------------------
def process_one_idea():
    images = [] # Inicijalizacija za bezbedno gašenje
    
    try:
        if not os.path.exists(config.IDEAS_FILE): 
            generate_ideas()
            
        with open(config.IDEAS_FILE, "r", encoding="utf-8") as f: 
            ideas = [line.strip() for line in f if line.strip()]
        
        if not ideas: 
            if not generate_ideas(): return 
            with open(config.IDEAS_FILE, "r", encoding="utf-8") as f: 
                ideas = [line.strip() for line in f if line.strip()]

        idea = ideas.pop(0)
        logger.info(f"🏛️ ARHITEKTONSKA OBRADA: {idea}")
        
        folder = os.path.join(config.BASE_OUTPUT_FOLDER, sanitize_filename(idea) + f"_{int(time.time())}")
        os.makedirs(os.path.join(folder, "slike"), exist_ok=True)
        os.makedirs(os.path.join(folder, "videi"), exist_ok=True)

        script = generate_script(idea)
        if not script: 
            with open(config.IDEAS_FILE, "a", encoding="utf-8") as f: f.write(idea + "\n")
            return
            
        # 🛠️ POPRAVKA: Validacija tačnog broja promptova (sprečava IndexError)
        if len(script.image_prompts) != 4 or len(script.video_prompts) != 4:
            logger.warning("⚠️ Skripta nije generisala tačno 4 prompta. Vraćam ideju nazad.")
            with open(config.IDEAS_FILE, "a", encoding="utf-8") as f: f.write(idea + "\n")
            return

        prompts_path = os.path.join(folder, "promptovi.txt")
        with open(prompts_path, "w", encoding="utf-8") as f:
            for i, p in enumerate(script.image_prompts): f.write(f"Image {i+1}: {p}\n\n")
            for i, p in enumerate(script.video_prompts): f.write(f"Video {i+1}: {p}\n\n")
        
        images = generate_images(script, os.path.join(folder, "slike"))
        
        # 🔥 ODMAH SNIMI YT METADATA
        if images and len(images) == 4:
            save_metadata(folder, script)
        else:
            logger.warning("⚠️ Generisanje slika puklo. Vraćam ideju u red.")
            with open(config.IDEAS_FILE, "a", encoding="utf-8") as f: f.write(idea + "\n")
            return
        
        with open(config.IDEAS_FILE, "w", encoding="utf-8") as f: 
            for item in ideas: f.write(item + "\n")
            
        with open(config.COMPLETED_IDEAS_FILE, "a", encoding="utf-8") as f:
            f.write(idea + f" - OBRAĐENO: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n")

        # 🔥 MANUAL MODE
        if config.SKIP_VIDEO_GENERATION:
            logger.info("⏭️ SKIP_VIDEO_GENERATION je True. Preskačem Veo 3.1 i montažu.")
            return

        transitions = [
            (images[0], images[1], script.video_prompts[0], 1), 
            (images[1], images[2], script.video_prompts[1], 2), 
            (images[2], images[3], script.video_prompts[2], 3), 
            (images[3], images[0], script.video_prompts[3], 4)
        ]

        for start, end, v_prompt, num in transitions:
            if not validate_progression(v_prompt):
                logger.warning(f"⚠️ Prompt za clip {num} nema '->'. Prekidam.")
                return
            path = os.path.join(folder, "videi", f"clip_{num}.mp4")
            if not generate_video_clip(start, end, v_prompt, path): return 
            time.sleep(config.INTER_VIDEO_DELAY)

        edit_video(folder, script)
        
    finally:
        # 🛠️ POPRAVKA: Čišćenje RAM-a od PIL slika bez obzira na ishod (memory leak fix)
        if images:
            for img in images:
                try: img.close()
                except: pass

if __name__ == "__main__":
    os.makedirs(config.BASE_OUTPUT_FOLDER, exist_ok=True)
    
    MAX_PROJECTS_PER_RUN = 5
    for current_run in range(1, MAX_PROJECTS_PER_RUN + 1):
        try:
            logger.info(f"🚀 Pokrećem projekat {current_run}/{MAX_PROJECTS_PER_RUN}...")
            process_one_idea()
            if current_run < MAX_PROJECTS_PER_RUN:
                logger.info("💤 Pauza 15 sekundi do sledećeg projekta...")
                time.sleep(15)
        except KeyboardInterrupt:
            logger.info("🛑 Skripta ručno prekinuta.")
            break
        except Exception as e:
            logger.error(f"CRITICAL ERROR: {e}")
            time.sleep(30)
            
    logger.info("✅ Produkcijska serija završena.")