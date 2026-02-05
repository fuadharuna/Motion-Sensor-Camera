
import os
import time
import smtplib
import ssl
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path
from gpiozero import MotionSensor

from picamera2 import Picamera2

PIR_GPIO_PIN = 17

CLIP_SECONDS = 15              
WARMUP_SECONDS = 2            
MIN_MOTION_GAP = 15            
MOTION_CONFIRM_WINDOW = 1.0    
OUTPUT_DIR = Path.home() / "motion_clips"

# Email settings from environment variables
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", SMTP_USER)
EMAIL_TO = os.environ.get("EMAIL_TO", "") 


def now_stamp() -> str:
    return datetime.now().strftime("%Y-%m-%d_%H-%M-%S")

def require_env():
    missing = []
    for k in ["SMTP_HOST", "SMTP_USER", "SMTP_PASS", "EMAIL_TO"]:
        if not os.environ.get(k):
            missing.append(k)
    if missing:
        raise RuntimeError(
            "Missing required environment variables: " + ", ".join(missing) + "\n"
            "Example:\n"
            "  export SMTP_HOST='smtp.gmail.com'\n"
            "  export SMTP_PORT='587'\n"
            "  export SMTP_USER='your_email@gmail.com'\n"
            "  export SMTP_PASS='your_app_password'\n"
            "  export EMAIL_TO='destination@gmail.com'\n"
        )

def send_email_with_attachment(subject: str, body: str, attachment_path: Path):
    recipients = [x.strip() for x in EMAIL_TO.split(",") if x.strip()]
    if not recipients:
        raise RuntimeError("EMAIL_TO is empty or invalid.")

    msg = EmailMessage()
    msg["From"] = EMAIL_FROM
    msg["To"] = ", ".join(recipients)
    msg["Subject"] = subject
    msg.set_content(body)

    data = attachment_path.read_bytes()
    # MP4 attachment
    msg.add_attachment(
        data,
        maintype="video",
        subtype="mp4",
        filename=attachment_path.name,
    )

    context = ssl.create_default_context()

    # Most SMTP providers use STARTTLS on 587
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)

def record_clip(picam2: Picamera2, out_path: Path, seconds: int):
    raw_h264 = out_path.with_suffix(".h264")

    picam2.start_recording(picam2.encoders.H264Encoder(bitrate=4_000_000), str(raw_h264))
    time.sleep(seconds)
    picam2.stop_recording()

    os.system(f"ffmpeg -y -loglevel error -r 30 -i '{raw_h264}' -c copy '{out_path}'")

    if out_path.exists() and out_path.stat().st_size > 0:
        try:
            raw_h264.unlink()
        except Exception:
            pass

def motion_confirmed(pir: MotionSensor, window_s: float) -> bool:
    """
    Helps reduce false triggers: requires motion to still be active within
    a short confirmation window.
    """
    start = time.time()
    while time.time() - start < window_s:
        if pir.motion_detected:
            return True
        time.sleep(0.05)
    return False


def main():
    require_env()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    pir = MotionSensor(PIR_GPIO_PIN)

    # Camera setup
    picam2 = Picamera2()
    video_config = picam2.create_video_configuration(
        main={"size": (1280, 720)}
    )
    picam2.configure(video_config)
    picam2.start()
    time.sleep(WARMUP_SECONDS)

    print("Motion sensor camera running.")
    print(f"Saving clips to: {OUTPUT_DIR}")
    print("Waiting for motion... (Ctrl+C to stop)")

    last_alert_time = 0.0

    try:
        while True:
            pir.wait_for_motion()

            # Cooldown to prevent spam
            now = time.time()
            if now - last_alert_time < MIN_MOTION_GAP:
                # Wait for motion to stop, then continue
                pir.wait_for_no_motion()
                continue

            # Confirm motion to reduce false triggers
            if not motion_confirmed(pir, MOTION_CONFIRM_WINDOW):
                pir.wait_for_no_motion()
                continue

            last_alert_time = time.time()

            ts = now_stamp()
            mp4_path = OUTPUT_DIR / f"motion_{ts}.mp4"

            print(f"[{ts}] Motion detected. Recording {CLIP_SECONDS}s -> {mp4_path.name}")
            record_clip(picam2, mp4_path, CLIP_SECONDS)

            subject = f"Motion Detected - {ts}"
            body = (
                f"Motion detected at {ts}.\n"
                f"Clip length: {CLIP_SECONDS}s\n"
                f"File: {mp4_path.name}\n"
            )

            try:
                send_email_with_attachment(subject, body, mp4_path)
                print(f"[{ts}] Email sent to {EMAIL_TO}")
            except Exception as e:
                print(f"[{ts}] Failed to send email: {e}")

            pir.wait_for_no_motion()

    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        try:
            picam2.stop()
        except Exception:
            pass


if __name__ == "__main__":
    main()
