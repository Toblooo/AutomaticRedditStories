import os
import time
import random
import subprocess
from gtts import gTTS
import glob
import whisper

# --- CONFIG ---
STORY_FILE = "story.txt"
TITLE_AUDIO = "title_narration.mp3"
BODY_AUDIO = "body_narration.mp3"
OUTPUT_AUDIO = "narration.mp3"
SUBTITLE_FILE = "subtitles.ass"  # ASS for karaoke
IMAGE_FOLDER = "images"
BACKGROUND_FOLDER = "backgrounds/"
OUTPUT_VIDEO = "output.mp4"
OUTPUT_RESOLUTION = "1080:1920"
OVERLAY_SCALE = "iw*min(864/iw\\,1):ih*min(864/iw\\,1)"
OVERLAY_Y_POSITION = "(main_h-overlay_h)/2"

# --- FONT / SUBTITLE TUNING ---
FONT_FOLDER = "fonts"            # Put Montserrat-ExtraBold.ttf here
FONT_NAME = "Montserrat ExtraBold"
FONT_SIZE = 84                   # bigger text (you can tweak)
MAX_WORDS_PER_LINE = 4           # fewer words on screen
OUTLINE_SIZE = 3                 # black stroke thickness
SHADOW_SIZE = 0
# Positive = delay subs; negative = show earlier. Adjust to taste.
SUB_TIMING_OFFSET = -0.12        # seconds; helps fix slight TTS/sub drift

# Minimal gap to guarantee no subs during title even if offset is negative
NO_SUBS_BEFORE = 0.05            # seconds after title ends


def format_ass_time(seconds: float) -> str:
    """ASS uses h:mm:ss.cs (centiseconds)"""
    seconds = max(0.0, seconds)
    cs = int(round((seconds - int(seconds)) * 100))
    s = int(seconds) % 60
    m = (int(seconds) // 60) % 60
    h = int(seconds) // 3600
    return f"{h:01}:{m:02}:{s:02}.{cs:02d}"


def _is_punct(token: str) -> bool:
    return token in {".", ",", "!", "?", ":", ";", "…", "'", "\"", ")", "]", "}", "—", "-", "–"}


def save_ass_subs(result, ass_path, title_duration):
    """
    Save ASS subtitles:
      - No subs during title
      - Words are white by default, turn yellow as spoken (karaoke)
      - 4-word chunks per line (MAX_WORDS_PER_LINE)
      - Black outline around letters
      - Timing offset applied to improve sync
    """
    with open(ass_path, "w", encoding="utf-8") as f:
        # Header
        f.write("[Script Info]\n")
        f.write("ScriptType: v4.00+\n")
        f.write("PlayResX: 1080\n")
        f.write("PlayResY: 1920\n")
        f.write("WrapStyle: 2\n\n")  # smart wrapping if it happens

        # Styles
        f.write("[V4+ Styles]\n")
        f.write("Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
                "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
                "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
                "Alignment, MarginL, MarginR, MarginV, Encoding\n")

        # IMPORTANT (ASS karaoke semantics):
        # - Highlighted portion uses PrimaryColour
        # - Unhighlighted portion uses SecondaryColour
        # We want base white, highlight yellow -> Primary = yellow, Secondary = white.
        # Color format is &HAABBGGRR (AA = alpha). Use AA=00 for opaque.
        PRIMARY_YELLOW = "&H00FFFF00"
        SECONDARY_WHITE = "&H00FFFFFF"
        OUTLINE_BLACK = "&H00000000"
        BACK_BLACK = "&H80000000"  # not used (no box), but harmless

        # Centered (5), outlined text
        f.write(
            "Style: Default,"
            f"{FONT_NAME},{FONT_SIZE},"
            f"{PRIMARY_YELLOW},{SECONDARY_WHITE},{OUTLINE_BLACK},{BACK_BLACK},"
            "0,0,0,0,100,100,0,0,1,"              # BorderStyle=1 (outline)
            f"{OUTLINE_SIZE},{SHADOW_SIZE},"
            "5,10,10,40,0\n\n"                    # Alignment=5 (center), MarginV=40
        )

        # Events
        f.write("[Events]\n")
        f.write("Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n")

        for seg in result["segments"]:
            # Skip anything that ends before or at the title end (raw times, no offset)
            if seg["end"] <= title_duration:
                continue

            if "words" not in seg or not seg["words"]:
                # Fallback: whole segment as one line after title
                start = max(title_duration + NO_SUBS_BEFORE, seg["start"] + SUB_TIMING_OFFSET)
                end = max(start + 0.01, seg["end"] + SUB_TIMING_OFFSET)
                f.write(f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{seg['text'].strip()}\n")
                continue

            words = seg["words"]

            # Chunk into MAX_WORDS_PER_LINE groups
            for i in range(0, len(words), MAX_WORDS_PER_LINE):
                chunk = words[i:i + MAX_WORDS_PER_LINE]
                raw_start = chunk[0]["start"]
                raw_end = chunk[-1]["end"]

                # Apply timing offset and clamp to avoid title period
                start = raw_start + SUB_TIMING_OFFSET
                end = raw_end + SUB_TIMING_OFFSET

                # If (after offset) the line would still end before title finishes, skip it
                if end <= title_duration:
                    continue

                # Ensure we don't show anything before title ends
                start = max(start, title_duration + NO_SUBS_BEFORE, 0.0)
                end = max(end, start + 0.01)

                # Build karaoke text: each token gets a \k duration (centiseconds)
                line = []
                first_token = True
                for idx, w in enumerate(chunk):
                    dur_cs = max(1, int(round((w["end"] - w["start"]) * 100)))
                    tok = (w.get("word") or "").strip()
                    if not tok:
                        continue

                    # spacing: add a space before non-punctuation tokens (except at line start)
                    if not first_token and not _is_punct(tok):
                        line.append(" ")

                    line.append(f"{{\\k{dur_cs}}}{tok}")
                    first_token = False

                text = "".join(line).strip()
                if text:
                    f.write(
                        f"Dialogue: 0,{format_ass_time(start)},{format_ass_time(end)},Default,,0,0,0,,{text}\n"
                    )


# --- 1) Load Story ---
try:
    with open(STORY_FILE, "r", encoding="utf-8") as f:
        story_text = f.read().strip()
except FileNotFoundError:
    print(f"❌ Error: {STORY_FILE} not found!")
    exit(1)

lines = story_text.split("\n", 1)
story_title = lines[0][:23]
story_body = lines[1] if len(lines) > 1 else ""

print("✅ Story loaded:", story_title)

# --- 2) Generate Narration (title + body) ---
try:
    tts_title = gTTS(story_title, lang="en")
    tts_title.save(TITLE_AUDIO)

    if story_body:
        tts_body = gTTS(story_body, lang="en")
        tts_body.save(BODY_AUDIO)

    concat_cmd = [
        "ffmpeg", "-y",
        "-i", TITLE_AUDIO,
        "-i", BODY_AUDIO if story_body else TITLE_AUDIO,
        "-filter_complex", "[0:a][1:a]concat=n=2:v=0:a=1[aout]" if story_body else "[0:a][0:a]concat=n=2:v=0:a=1[aout]",
        "-map", "[aout]",
        OUTPUT_AUDIO
    ]
    subprocess.run(concat_cmd, check=True)

    # Duration of the title clip (for subtitle skipping)
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

# --- 3) Transcribe to ASS karaoke subs ---
try:
    model = whisper.load_model("small.en")
    result = model.transcribe(OUTPUT_AUDIO, word_timestamps=True)  # per-word timing
    save_ass_subs(result, SUBTITLE_FILE, title_duration)
    print("✅ Subtitles saved as", SUBTITLE_FILE)
except Exception as e:
    print(f"❌ Error generating subtitles: {str(e)}")
    exit(1)

# --- 4) Pick PNG ---
png_files = glob.glob(os.path.join(IMAGE_FOLDER, "*.png"))
if not png_files:
    print(f"❌ Error: No PNG files found in {IMAGE_FOLDER}.")
    exit(1)
TITLE_IMAGE = max(png_files, key=os.path.getmtime)
print("✅ Using PNG:", TITLE_IMAGE)

# --- 5) Pick Background Video ---
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

# --- 6) Merge with FFmpeg ---
subtitle_path = os.path.abspath(SUBTITLE_FILE).replace("\\", "/").replace(":", r"\\:")
font_dir = os.path.abspath(FONT_FOLDER).replace("\\", "/").replace(":", r"\\:")

cmd = [
    "ffmpeg", "-y", "-loglevel", "debug",
    "-i", bg_video,
    "-i", OUTPUT_AUDIO,
    "-i", TITLE_IMAGE,
    "-filter_complex",
    f"[0:v]scale=iw*max(1080/iw\\,1920/ih):ih*max(1080/iw\\,1920/ih),crop=1080:1920[v0];"
    f"[2:v]scale={OVERLAY_SCALE}[v2];"
    f"[v0][v2]overlay=(main_w-overlay_w)/2:{OVERLAY_Y_POSITION}:enable='between(t,0,{title_duration})':format=auto[vout];"
    f"[vout]subtitles=filename={subtitle_path}:fontsdir={font_dir}[vfinal]",
    "-map", "[vfinal]",
    "-map", "1:a",
    "-shortest",
    "-c:v", "libx264",
    "-c:a", "aac",
    "-b:a", "192k",
    "-ar", "44100",
    "-pix_fmt", "yuv420p",
    OUTPUT_VIDEO
]

try:
    subprocess.run(cmd, check=True)
    print("✅ Final video saved as", OUTPUT_VIDEO)
except subprocess.CalledProcessError as e:
    print(f"❌ Error in FFmpeg processing: {str(e)}")
    exit(1)

# --- 7) Cleanup ---
try:
    os.remove(TITLE_AUDIO)
    if story_body:
        os.remove(BODY_AUDIO)
    os.remove(SUBTITLE_FILE)
except:
    pass
