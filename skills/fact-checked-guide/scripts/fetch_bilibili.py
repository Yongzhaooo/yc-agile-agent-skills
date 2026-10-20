#!/usr/bin/env python3
"""
Fetch Bilibili video metadata, subtitles, or audio stream for transcription.
Outputs structured JSON, plain text, or initial markdown notes.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import random
import re
import string
import sys
import time
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

MIXIN_KEY_ENC_TAB = [
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
    33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40,
    61, 26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11,
    36, 20, 34, 44, 52,
]

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


class BilibiliFetcher:
    def __init__(self, cookie: Optional[str] = None):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": UA,
            "Referer": "https://www.bilibili.com/",
        })
        if cookie:
            self.session.headers["Cookie"] = cookie

    def resolve_url(self, url: str) -> str:
        """Resolve b23.tv short URLs if needed."""
        url = url.strip()
        if "b23.tv" in url:
            try:
                resp = self.session.head(url, allow_redirects=True, timeout=10)
                return resp.url
            except Exception as exc:
                print(f"[warning] Failed to resolve short URL {url}: {exc}", file=sys.stderr)
        return url

    def extract_bvid_and_p(self, url_or_bvid: str) -> Tuple[str, int]:
        target = self.resolve_url(url_or_bvid)
        bv_match = re.search(r"BV([0-9A-Za-z]+)", target, re.IGNORECASE)
        if not bv_match:
            raise ValueError(f"Could not extract BV id from '{url_or_bvid}'")
        bvid = f"BV{bv_match.group(1)}"

        # Check p parameter
        p_match = re.search(r"[?&]p=(\d+)", target)
        p = int(p_match.group(1)) if p_match else 1
        return bvid, p

    def fetch_video_page_info(self, bvid: str, p: int = 1) -> Dict[str, Any]:
        """Fetch video page HTML and extract metadata and session cookies."""
        url = f"https://www.bilibili.com/video/{bvid}/"
        resp = self.session.get(url, timeout=15)
        resp.raise_for_status()

        # Extract window.__INITIAL_STATE__
        match = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\});", resp.text)
        if not match:
            raise RuntimeError(f"Could not find window.__INITIAL_STATE__ for {bvid}")

        data = json.loads(match.group(1))
        video_data = data.get("videoData", {})
        pages = video_data.get("pages", [])

        cid = video_data.get("cid")
        page_title = video_data.get("title", "")
        duration = video_data.get("duration", 0)

        if pages:
            if 1 <= p <= len(pages):
                current_page = pages[p - 1]
                cid = current_page.get("cid", cid)
                if len(pages) > 1 and current_page.get("part"):
                    page_title = f"{page_title} - P{p} {current_page.get('part')}"
                duration = current_page.get("duration", duration)
            else:
                current_page = pages[0]
                cid = current_page.get("cid", cid)
                duration = current_page.get("duration", duration)

        owner = video_data.get("owner", {})
        return {
            "bvid": bvid,
            "aid": video_data.get("aid"),
            "cid": cid,
            "p": p,
            "title": page_title,
            "owner_name": owner.get("name", ""),
            "owner_mid": owner.get("mid"),
            "desc": video_data.get("desc", ""),
            "duration": duration,
            "pubdate": video_data.get("pubdate"),
            "pic": video_data.get("pic", ""),
        }

    def fetch_subtitles(self, bvid: str, cid: int) -> Optional[Dict[str, Any]]:
        """Try fetching official/AI subtitles via player API."""
        url = "https://api.bilibili.com/x/player/wbi/v2"
        try:
            resp = self.session.get(url, params={"bvid": bvid, "cid": cid}, timeout=10)
            data = resp.json()
        except Exception as exc:
            print(f"[warning] Failed to query subtitle API: {exc}", file=sys.stderr)
            return None

        if data.get("code") != 0:
            return None

        subtitles = data.get("data", {}).get("subtitle", {}).get("subtitles", [])
        if not subtitles:
            return None

        # Pick best subtitle track (human zh > ai zh > any zh > first)
        def is_zh(s: dict) -> bool:
            lan = (s.get("lan") or "").lower()
            return lan.startswith("zh") or lan == "ai-zh"

        selected = None
        for s in subtitles:
            if is_zh(s) and not s.get("ai_type"):
                selected = s
                break
        if not selected:
            for s in subtitles:
                if is_zh(s):
                    selected = s
                    break
        if not selected:
            selected = subtitles[0]

        sub_url = selected.get("subtitle_url")
        if not sub_url:
            return None

        if sub_url.startswith("//"):
            sub_url = "https:" + sub_url

        try:
            sub_resp = self.session.get(sub_url, timeout=15)
            sub_data = sub_resp.json()
        except Exception as exc:
            print(f"[warning] Failed to download subtitle body: {exc}", file=sys.stderr)
            return None

        body = sub_data.get("body", [])
        segments = []
        for item in body:
            txt = (item.get("content") or "").strip()
            if not txt:
                continue
            segments.append({
                "start": round(float(item.get("from", 0)), 2),
                "end": round(float(item.get("to", 0)), 2),
                "text": txt,
            })

        if not segments:
            return None

        return {
            "source": "official_subtitle" if not selected.get("ai_type") else "ai_subtitle",
            "lan": selected.get("lan", "zh"),
            "lan_doc": selected.get("lan_doc", ""),
            "segments": segments,
            "full_text": " ".join(s["text"] for s in segments),
        }

    def _get_wbi_mixin_key(self) -> str:
        nav = self.session.get("https://api.bilibili.com/x/web-interface/nav", timeout=10).json()
        wbi_img = nav.get("data", {}).get("wbi_img", {})
        img_key = wbi_img.get("img_url", "").split("/")[-1].split(".")[0]
        sub_key = wbi_img.get("sub_url", "").split("/")[-1].split(".")[0]
        raw_key = img_key + sub_key
        return "".join([raw_key[n] for n in MIXIN_KEY_ENC_TAB])[:32]

    def fetch_audio_stream_url(self, bvid: str, cid: int) -> Optional[str]:
        """Fetch direct DASH audio stream URL using WBI and dm_img params."""
        try:
            mixin_key = self._get_wbi_mixin_key()
            dm_img_str = base64.b64encode(
                "".join(random.choices(string.printable, k=30)).encode()
            )[:-2].decode()
            dm_cover_img_str = base64.b64encode(
                "".join(random.choices(string.printable, k=40)).encode()
            )[:-2].decode()

            params = {
                "bvid": bvid,
                "cid": cid,
                "fnval": 16,  # DASH
                "fnver": 0,
                "fourk": 1,
                "web_location": 1550101,
                "dm_img_list": "[]",
                "dm_img_str": dm_img_str,
                "dm_cover_img_str": dm_cover_img_str,
                "dm_img_inter": json.dumps({"ds": [], "wh": [6093, 6631, 31], "of": [430, 760, 380]}),
                "wts": int(time.time()),
            }
            sorted_params = dict(sorted(params.items()))
            query = urllib.parse.urlencode(sorted_params)
            params["w_rid"] = hashlib.md5((query + mixin_key).encode()).hexdigest()

            resp = self.session.get(
                "https://api.bilibili.com/x/player/wbi/playurl",
                params=params,
                timeout=15,
            ).json()

            if resp.get("code") != 0:
                print(f"[warning] playurl returned code {resp.get('code')}: {resp.get('message')}", file=sys.stderr)
                return None

            dash = resp.get("data", {}).get("dash", {})
            audios = dash.get("audio", [])
            if audios:
                return audios[0].get("baseUrl") or audios[0].get("base_url")
        except Exception as exc:
            print(f"[warning] Failed to fetch audio stream url: {exc}", file=sys.stderr)

        return None

    def transcribe_with_bcut(self, audio_data: bytes) -> Optional[Dict[str, Any]]:
        """Transcribe audio using Bilibili BCut (必剪) ASR API."""
        api_base = "https://member.bilibili.com/x/bcut/rubick-interface"
        headers = {
            "User-Agent": "Bilibili/1.0.0 (https://www.bilibili.com)",
            "Content-Type": "application/json",
        }

        # 1. Request upload
        payload = json.dumps({
            "type": 2,
            "name": "audio.m4a",
            "size": len(audio_data),
            "ResourceFileType": "m4a",
            "model_id": "8",
        })
        resp = self.session.post(f"{api_base}/resource/create", data=payload, headers=headers, timeout=20).json()
        if resp.get("code") != 0:
            print(f"[warning] BCut create upload failed: {resp}", file=sys.stderr)
            return None

        data = resp.get("data", {})
        in_boss_key = data["in_boss_key"]
        resource_id = data["resource_id"]
        upload_id = data["upload_id"]
        upload_urls = data["upload_urls"]
        per_size = data["per_size"]

        # 2. Upload parts
        etags = []
        for i, up_url in enumerate(upload_urls):
            start = i * per_size
            end = min((i + 1) * per_size, len(audio_data))
            chunk_data = audio_data[start:end]
            etag = None
            for attempt in range(3):
                try:
                    part_resp = self.session.put(
                        up_url,
                        data=chunk_data,
                        headers={"Content-Type": "application/octet-stream"},
                        timeout=120,
                    )
                    part_resp.raise_for_status()
                    etag = part_resp.headers.get("Etag", "").strip('"')
                    break
                except Exception as exc:
                    print(f"[warning] Upload chunk {i} attempt {attempt+1} failed: {exc}", file=sys.stderr)
                    time.sleep(2)
            if not etag:
                print(f"[warning] Failed to upload chunk {i} after 3 attempts", file=sys.stderr)
                return None
            etags.append(etag)

        # 3. Complete upload
        commit_data = json.dumps({
            "InBossKey": in_boss_key,
            "ResourceId": resource_id,
            "Etags": ",".join(etags),
            "UploadId": upload_id,
            "model_id": "8",
        })
        commit_resp = self.session.post(
            f"{api_base}/resource/create/complete",
            data=commit_data,
            headers=headers,
            timeout=30,
        ).json()
        if commit_resp.get("code") != 0:
            print(f"[warning] BCut complete upload failed: {commit_resp}", file=sys.stderr)
            return None

        download_url = commit_resp.get("data", {}).get("download_url")

        # 4. Create task
        task_resp = self.session.post(
            f"{api_base}/task",
            json={"resource": download_url, "model_id": "8"},
            headers=headers,
            timeout=30,
        ).json()
        if task_resp.get("code") != 0:
            print(f"[warning] BCut create task failed: {task_resp}", file=sys.stderr)
            return None

        task_id = task_resp.get("data", {}).get("task_id")
        print(f"[info] BCut task created: {task_id}, polling for results...", file=sys.stderr)

        # 5. Poll results
        for _ in range(180):
            time.sleep(2)
            query_resp = self.session.get(
                f"{api_base}/task/result",
                params={"model_id": "7", "task_id": task_id},
                headers=headers,
                timeout=20,
            ).json()
            if query_resp.get("code") != 0:
                continue
            q_data = query_resp.get("data", {})
            state = q_data.get("state")
            if state == 4:  # SUCCESS
                result_json = json.loads(q_data.get("result", "{}"))
                segments = []
                full_texts = []
                for u in result_json.get("utterances", []):
                    t = u.get("transcript", "").strip()
                    if not t:
                        continue
                    full_texts.append(t)
                    segments.append({
                        "start": round(float(u.get("start_time", 0)) / 1000.0, 2),
                        "end": round(float(u.get("end_time", 0)) / 1000.0, 2),
                        "text": t,
                    })
                return {
                    "source": "bcut_asr",
                    "lan": result_json.get("language", "zh"),
                    "segments": segments,
                    "full_text": " ".join(full_texts),
                }
            elif state == 3:  # FAILED
                print(f"[warning] BCut ASR task failed: {q_data}", file=sys.stderr)
                return None

        return None

    def transcribe_with_whisper(self, audio_data: bytes) -> Optional[Dict[str, Any]]:
        """Fallback local transcription using faster-whisper."""
        try:
            import tempfile
            from faster_whisper import WhisperModel
            with tempfile.NamedTemporaryFile(suffix=".m4a", delete=False) as tmp:
                tmp.write(audio_data)
                tmp_path = tmp.name
            try:
                print(f"[info] Loading faster-whisper-large-v3-turbo on CPU...", file=sys.stderr)
                model = WhisperModel("mobiuslabsgmbh/faster-whisper-large-v3-turbo", device="cpu", compute_type="int8")
                print(f"[info] Transcribing audio with local Whisper...", file=sys.stderr)
                segments_gen, info = model.transcribe(tmp_path, beam_size=1, language="zh")
                segments = []
                full_texts = []
                for s in segments_gen:
                    t = s.text.strip()
                    if t:
                        full_texts.append(t)
                        segments.append({
                            "start": round(s.start, 2),
                            "end": round(s.end, 2),
                            "text": t,
                        })
                return {
                    "source": "faster_whisper_local",
                    "lan": info.language or "zh",
                    "segments": segments,
                    "full_text": " ".join(full_texts),
                }
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
        except Exception as exc:
            print(f"[warning] Local Whisper transcription failed: {exc}", file=sys.stderr)
            return None


def run_fetch(
    url_or_bvid: str,
    p: int = 1,
    transcribe_if_needed: bool = True,
    cookie: Optional[str] = None,
) -> Dict[str, Any]:
    fetcher = BilibiliFetcher(cookie=cookie)
    bvid, parsed_p = fetcher.extract_bvid_and_p(url_or_bvid)
    effective_p = parsed_p if parsed_p > 1 else p

    print(f"[info] Fetching metadata for {bvid} (P{effective_p})...", file=sys.stderr)
    info = fetcher.fetch_video_page_info(bvid, p=effective_p)

    cid = info.get("cid")
    transcript_data = None
    if cid:
        print(f"[info] Checking subtitles for {bvid} (cid={cid})...", file=sys.stderr)
        transcript_data = fetcher.fetch_subtitles(bvid, cid)

    audio_url = None
    if not transcript_data and cid:
        print(f"[info] No official/AI subtitles found. Fetching audio stream URL...", file=sys.stderr)
        audio_url = fetcher.fetch_audio_stream_url(bvid, cid)
        info["audio_url"] = audio_url

        if transcribe_if_needed and audio_url:
            print(f"[info] Downloading audio stream for transcription...", file=sys.stderr)
            audio_bytes = b""
            for attempt in range(6):
                headers = {"Referer": "https://www.bilibili.com/"}
                if len(audio_bytes) > 0:
                    headers["Range"] = f"bytes={len(audio_bytes)}-"
                try:
                    with fetcher.session.get(
                        audio_url,
                        headers=headers,
                        stream=True,
                        timeout=90,
                    ) as audio_resp:
                        if audio_resp.status_code in (200, 206):
                            for chunk in audio_resp.iter_content(chunk_size=65536):
                                if chunk:
                                    audio_bytes += chunk
                            cr = audio_resp.headers.get("Content-Range", "")
                            if "/" in cr:
                                try:
                                    total_len = int(cr.split("/")[-1])
                                    if len(audio_bytes) >= total_len:
                                        break
                                except ValueError:
                                    pass
                            elif audio_resp.status_code == 200:
                                break
                except Exception as exc:
                    print(f"[warning] Audio download attempt {attempt+1} failed ({exc}), resuming at {len(audio_bytes)} bytes...", file=sys.stderr)
                    time.sleep(2)

            if len(audio_bytes) > 1024:
                print(f"[info] Audio downloaded ({len(audio_bytes) // 1024} KB). Submitting to BCut ASR...", file=sys.stderr)
                try:
                    transcript_data = fetcher.transcribe_with_bcut(audio_bytes)
                except Exception as e:
                    print(f"[warning] BCut failed ({e}), falling back to local Whisper...", file=sys.stderr)
                if not transcript_data:
                    print(f"[info] Running local faster-whisper fallback...", file=sys.stderr)
                    transcript_data = fetcher.transcribe_with_whisper(audio_bytes)

    result: Dict[str, Any] = {
        "metadata": info,
        "has_transcript": transcript_data is not None,
    }
    if transcript_data:
        result["transcript"] = transcript_data
    elif audio_url:
        result["audio_url"] = audio_url

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Bilibili metadata and subtitles/transcription.")
    parser.add_argument("url", help="Bilibili video URL, b23.tv link, or BV id")
    parser.add_argument("--p", type=int, default=1, help="Part/P number (default 1)")
    parser.add_argument("--output", "-o", help="Save output to file (JSON or MD)")
    parser.add_argument("--no-transcribe", action="store_true", help="Do not run ASR if subtitle is missing")
    parser.add_argument("--format", choices=["json", "text", "md"], default="json", help="Output format")
    parser.add_argument("--cookie", default=os.getenv("BILIBILI_COOKIE"), help="Optional Bilibili cookie string")

    args = parser.parse_args()

    try:
        data = run_fetch(
            args.url,
            p=args.p,
            transcribe_if_needed=not args.no_transcribe,
            cookie=args.cookie,
        )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    # Formatting
    output_str = ""
    if args.format == "json":
        output_str = json.dumps(data, ensure_ascii=False, indent=2)
    elif args.format == "text":
        meta = data.get("metadata", {})
        output_str += f"Title: {meta.get('title')}\nAuthor: {meta.get('owner_name')}\nBV: {meta.get('bvid')}\n\n"
        if data.get("has_transcript"):
            output_str += data["transcript"].get("full_text", "")
        else:
            output_str += "No transcript available."
    elif args.format == "md":
        meta = data.get("metadata", {})
        output_str += f"# {meta.get('title', 'Video')}\n\n"
        output_str += f"- **UP 主**: {meta.get('owner_name')}\n"
        output_str += f"- **BV 号**: [{meta.get('bvid')}](https://www.bilibili.com/video/{meta.get('bvid')})\n"
        output_str += f"- **时长**: {meta.get('duration')}s\n"
        output_str += f"- **简介**: {meta.get('desc')}\n\n"
        if data.get("has_transcript"):
            tr = data["transcript"]
            output_str += f"## 原始字幕/转录内容 ({tr.get('source')})\n\n"
            for seg in tr.get("segments", []):
                output_str += f"- `[{seg.get('start')}s - {seg.get('end')}s]` {seg.get('text')}\n"
        else:
            output_str += "*(未获取到转录文本)*\n"

    if args.output:
        out_path = Path(args.output).resolve()
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output_str, encoding="utf-8")
        print(f"Written to {out_path}", file=sys.stderr)
    else:
        # Reconfigure sys.stdout to utf-8 if printing directly
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except Exception:
            pass
        print(output_str)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
