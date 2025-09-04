import os
import time
import random
import subprocess
from gtts import gTTS
import glob
import tempfile
import whisper

# --- CONFIG ---
STORY_FILE = "story.txt"
TITLE_AUDIO = "title_narration.mp3"
BODY_AUDIO = "body_narration.mp3"
OUTPUT_AUDIO = "narration.mp3"
SUBTITLE_FILE = "subtitles.srt"
IMAGE_FOLDER = "images"  # folder where you put your PNG
BACKGROUND_FOLDER = "backgrounds/"
OUTPUT_VIDEO = "output.mp4"
OUTPUT_RESOLUTION = "1080:1920"  # Phone format resolution
OVERLAY_SCALE = "iw*min(864/iw\\,1):ih*min(864/iw\\,1)"  # Constrain width to max 864px, preserve aspect ratio
OVERLAY_Y_POSITION = "(main_h-overlay_h)/2"  # Center PNG vertically
SUBTITLE_STYLE = "FontName=Arial,Fontsize=40,PrimaryColour=&HFFFFFF,OutlineColour=&H000000,BorderStyle=3,Outline=3,Shadow=0,Alignment=2,MarginV=100"

def format_srt_time(seconds: float) -> str:
    ms = int((seconds % 1) * 1000)
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:02}:{m:02}:{s:02},{ms:03}"

# --- 1. Load Story ---
try:
    with open(STORY_FILE, "r", encoding="utf-8") as f:
        story_text = f.read().strip()
except FileNotFoundError:
    print(f"❌ Error: {STORY_FILE} not found!")
    exit(1)

lines = story_text.split("\n", 1)
story_title = lines[0][:23]  # Limit to 23 chars if needed
story_body = lines[1] if len(lines) > 1 else ""

print("✅ Story loaded:", story_title)

# --- 2. Generate Narration ---
try:
    # Generate title narration
    tts_title = gTTS(story_title, lang="en")
    tts_title.save(TITLE_AUDIO)
    
    # Generate body narration
    if story_body:
        tts_body = gTTS(story_body, lang="en")
        tts_body.save(BODY_AUDIO)
    
    # Concatenate audio files
    concat_cmd = [
        "ffmpeg", "-y",
        "-i", TITLE_AUDIO,
        "-i", BODY_AUDIO if story_body else TITLE_AUDIO,  # Use title audio twice if no body
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[aout]" if story_body else "[0:a][0:a]concat=n=2:v=0:a=1[aout]",
        "-map", "[aout]",
        OUTPUT_AUDIO
    ]
    subprocess.run(concat_cmd, check=True)
    
    # Get title audio duration for overlay timing
    duration_cmd = [
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        TITLE_AUDIO
    ]
    title_duration = float(subprocess.check_output(duration_cmd).decode().strip())
    
    print("✅ Narration saved as", OUTPUT_AUDIO)
except Exception as e:
    print(f"❌ Error generating narration: {str(e)}")
    exit(1)

# --- 3. Generate Subtitles with Whisper ---
try:
    model = whisper.load_model("small.en")  # Use small English model for faster processing
    result = model.transcribe(OUTPUT_AUDIO, word_timestamps=False)
    print("🔍 Whisper transcription segments:", result["segments"])  # Debug transcription
    with open(SUBTITLE_FILE, "w", encoding="utf-8") as f:
        for i, segment in enumerate(result["segments"], 1):
            start = segment["start"]
            end = segment["end"]
            text = segment["text"].strip()
            f.write(f"{i}\n")
            f.write(f"{format_srt_time(start)} --> {format_srt_time(end)}\n")
            f.write(f"{text}\n\n")
    if os.path.exists(SUBTITLE_FILE):
        print(f"✅ Subtitles file exists at: {os.path.abspath(SUBTITLE_FILE)}")
        with open(SUBTITLE_FILE, "r", encoding="utf-8") as f:
            print(f"🔍 Subtitles content:\n{f.read()}")
    else:
        print(f"❌ Subtitles file not found: {SUBTITLE_FILE}")
        exit(1)
    print("✅ Subtitles saved as", SUBTITLE_FILE)
except Exception as e:
    print(f"❌ Error generating subtitles: {str(e)}")
    exit(1)

# --- 4. Get PNG from folder ---
png_files = glob.glob(os.path.join(IMAGE_FOLDER, "*.png"))
if not png_files:
    print(f"❌ Error: No PNG files found in {IMAGE_FOLDER}. Please add one and try again.")
    exit(1)

TITLE_IMAGE = max(png_files, key=os.path.getmtime)
print("✅ Using PNG:", TITLE_IMAGE)

# --- 5. Pick Background Video ---
try:
    bg_video = random.choice([
        os.path.join(BACKGROUND_FOLDER, f)
        for f in os.listdir(BACKGROUND_FOLDER)
        if f.endswith(".mp4")
    ])
    print("🎥 Using background video:", bg_video)
except FileNotFoundError:
    print(f"❌ Error: No .mp4 files found in {BACKGROUND_FOLDER}")
    exit(1)

# --- 6. Merge with FFmpeg ---
# Use absolute path for subtitles.srt with forward slashes
subtitle_path = os.path.abspath(SUBTITLE_FILE).replace("\\", "/").replace(":", r"\\:")
cmd = [
    "ffmpeg", "-y", "-loglevel", "debug",
    "-i", bg_video,       # background video
    "-i", OUTPUT_AUDIO,   # narration
    "-i", TITLE_IMAGE,    # PNG overlay
    "-filter_complex",
    f"[0:v]scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),crop=1080:1920[v0];"
    f"[2:v]scale={OVERLAY_SCALE}[v2];"
    f"[v0][v2]overlay=(main_w-overlay_w)/2:{OVERLAY_Y_POSITION}:enable='between(t,0,{title_duration})':format=auto[vout];"
    f"[vout]subtitles=filename={subtitle_path}:force_style='{SUBTITLE_STYLE}'[vfinal]",
    "-map", "[vfinal]",   # use processed video with subtitles
    "-map", "1:a",        # use narration audio
    "-shortest",
    "-c:v", "libx264",
    "-c:a", "aac",
    "-b:a", "192k",       # Set audio bitrate for better quality
    "-ar", "44100",       # Set audio sample rate
    "-pix_fmt", "yuv420p", # Ensure compatibility
    OUTPUT_VIDEO
]

try:
    subprocess.run(cmd, check=True)
    print("✅ Final video saved as", OUTPUT_VIDEO)
except subprocess.CalledProcessError as e:
    print(f"❌ Error in FFmpeg processing: {str(e)}")
    exit(1)

# Clean up temporary files
try:
    os.remove(TITLE_AUDIO)
    if story_body:
        os.remove(BODY_AUDIO)
    os.remove(SUBTITLE_FILE)
except:
    pass