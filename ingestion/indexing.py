"""
ingestion/indexing.py
Purpose: Pure-Python parser and chunker that transforms time-coded HTML transcripts
into structured Vertex AI Search records (structData) with agenda-boundary grouping,
timestamped deep-links, and numeric POSIX sort_timestamp values.
BoM: Modular Transcript Parser & Record Formatter.
"""
import datetime
import logging
import re

logger = logging.getLogger(__name__)


def parse_transcript_html(html_content):
    """
    Parses transcript.html and extracts meeting metadata, agenda items, and speaker utterances.
    Returns dict: {'title', 'date', 'index_target', 'utterances', 'agendas'}.
    """
    title_match = re.search(r'<h2 id="meeting-title">(.*?)</h2>', html_content)
    title = title_match.group(1).strip() if title_match else "Unknown Meeting"

    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", title)
    date = date_match.group(1) if date_match else "Unknown Date"

    agendas = []
    agenda_nav = re.search(
        r'<nav id="agenda">(.*?)</nav>', html_content, re.DOTALL | re.IGNORECASE
    )
    if agenda_nav:
        nav_content = agenda_nav.group(1)
        list_items = re.findall(r"<li>(.*?)</li>", nav_content, re.DOTALL)
        for li in list_items:
            a_match = re.search(r'<a href="#item-(\d+)">(.*)\s+\((.*?)\)</a>', li)
            if a_match:
                agenda_item = a_match.group(1).strip()
                agenda_title = a_match.group(2).strip()
                time_str = a_match.group(3).strip()
                parts = time_str.split(":")
                try:
                    if len(parts) == 3:
                        secs = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
                    elif len(parts) == 2:
                        secs = int(parts[0]) * 60 + int(parts[1])
                    else:
                        secs = float(time_str)
                    agendas.append(
                        {
                            "title": agenda_title,
                            "item_number": agenda_item,
                            "start_time": float(secs),
                        }
                    )
                except Exception:
                    pass
    agendas.sort(key=lambda x: x["start_time"])

    paragraphs = re.findall(r"<p>(.*?)</p>", html_content, re.DOTALL)
    utterances = []
    for p_content in paragraphs:
        speaker_match = re.search(r"<strong>\[(.*?)\]:</strong>", p_content)
        current_speaker = speaker_match.group(1) if speaker_match else "Unknown"

        spans = re.findall(
            r'<span class="utterance" data-start-time="([\d\.]+)">(.*?)</span>',
            p_content,
            re.DOTALL,
        )
        for start_time, text in spans:
            utterances.append(
                {
                    "text": text.strip(),
                    "start": float(start_time),
                    "speaker": current_speaker,
                }
            )

    index_target = "Unknown"
    jurisdiction_match = re.search(
        r">\s*([^<]+?)\s+SmartTranscripts\s*<", html_content, re.IGNORECASE
    )
    if jurisdiction_match:
        index_target = jurisdiction_match.group(1).strip()

    return {
        "title": title,
        "date": date,
        "index_target": index_target,
        "utterances": utterances,
        "agendas": agendas,
    }


def group_utterances_into_clips(utterances, meeting_metadata, chunk_duration=45):
    """Groups raw utterances into fixed-duration time clips when no agenda items exist."""
    if not utterances:
        return []

    clips = []
    current_clip = {
        "text": [],
        "start": utterances[0]["start"],
        "end": utterances[0]["start"],
        "speakers": set(),
    }

    for i, u in enumerate(utterances):
        end_time = (
            utterances[i + 1]["start"]
            if i + 1 < len(utterances)
            else u["start"] + 5.0
        )

        if u["start"] - current_clip["start"] > chunk_duration:
            clips.append(
                {
                    "text": " ".join(current_clip["text"]),
                    "start": current_clip["start"],
                    "end": current_clip["end"],
                    "speakers": list(current_clip["speakers"]),
                    "meeting_metadata": meeting_metadata,
                }
            )
            current_clip = {
                "text": [u["text"]],
                "start": u["start"],
                "end": end_time,
                "speakers": {u["speaker"]},
            }
        else:
            current_clip["text"].append(u["text"])
            current_clip["end"] = end_time
            current_clip["speakers"].add(u["speaker"])

    if current_clip["text"]:
        clips.append(
            {
                "text": " ".join(current_clip["text"]),
                "start": current_clip["start"],
                "end": current_clip["end"],
                "speakers": list(current_clip["speakers"]),
                "meeting_metadata": meeting_metadata,
            }
        )

    return clips


def group_utterances_by_agenda(utterances, agendas, meeting_metadata, fallback_chunk=300):
    """Groups utterances into semantic clips aligned with agenda item start times."""
    if not utterances:
        return []

    if not agendas:
        return group_utterances_into_clips(
            utterances, meeting_metadata, chunk_duration=fallback_chunk
        )

    clips = []
    utterance_idx = 0
    num_utterances = len(utterances)

    for i, agenda in enumerate(agendas):
        agenda_start = 0.0 if i == 0 else agenda["start_time"]
        agenda_end = (
            agendas[i + 1]["start_time"] if i + 1 < len(agendas) else float("inf")
        )

        current_clip = {
            "text": [],
            "start": agenda_start,
            "end": agenda_start,
            "speakers": set(),
            "agenda_title": agenda["title"],
            "agenda_item": agenda.get("item_number", ""),
        }

        has_utterances = False
        while utterance_idx < num_utterances:
            u = utterances[utterance_idx]
            if u["start"] < agenda_end:
                current_clip["text"].append(u["text"])
                current_clip["end"] = u["start"] + 5.0
                current_clip["speakers"].add(u["speaker"])
                has_utterances = True
                utterance_idx += 1
            else:
                break

        if has_utterances:
            clips.append(
                {
                    "text": " ".join(current_clip["text"]),
                    "start": (
                        current_clip["start"]
                        if current_clip["start"] > 0
                        else utterances[0]["start"]
                    ),
                    "end": current_clip["end"],
                    "speakers": list(current_clip["speakers"]),
                    "agenda_title": agenda["title"],
                    "agenda_item": agenda.get("item_number", ""),
                    "meeting_metadata": meeting_metadata,
                }
            )

    return clips


def format_indexing_record(
    clip, jurisdiction, chamber, committee, meeting_folder, rel_path=None
):
    """Formats a grouped clip into a structured Vertex AI Search document dictionary."""
    m_metadata = clip["meeting_metadata"]
    date_str = m_metadata.get("date", "Unknown Date")
    meeting_time = "00:00"
    sort_ts = float(clip["start"])

    folder_match = re.search(
        r"(\d{4}[-_]\d{2}[-_]\d{2})(?:[-_]?(\d{2}[-:]\d{2}))?", meeting_folder
    )
    if folder_match:
        date_str = folder_match.group(1).replace("_", "-")
        raw_time = folder_match.group(2)
        if raw_time:
            meeting_time = raw_time.replace("-", ":")

    if date_str != "Unknown Date":
        try:
            dt_obj = datetime.datetime.strptime(
                f"{date_str} {meeting_time}", "%Y-%m-%d %H:%M"
            )
            sort_ts = dt_obj.timestamp() + clip["start"]
        except Exception:
            pass

    m_metadata["date"] = date_str

    if not rel_path:
        if chamber == "meetings":
            rel_path = f"/{chamber}/{committee}/{meeting_folder}/transcript.html"
        else:
            rel_path = f"/meetings/{chamber}/{committee}/{meeting_folder}/transcript.html"

    deep_link = f"{rel_path}?startTime={clip['start']}&endTime={clip['end']}"

    def sanitize(s):
        """Normalizes a path segment into lowercase alphanumeric and underscores."""
        return "".join(c if c.isalnum() or c in "-_" else "_" for c in s).lower()

    clip_id = (
        f"{sanitize(chamber)}_{sanitize(committee)}_"
        f"{sanitize(meeting_folder)}_{int(clip['start'])}"
    )

    agenda_title = clip.get("agenda_title", "")
    agenda_item = clip.get("agenda_item", "")

    content_payload = clip["text"]
    meeting_title = m_metadata.get("title", "Unknown Meeting")
    if agenda_title:
        content_payload = f"Agenda Item: {agenda_title}\n\n{content_payload}"
        meeting_title = f"{meeting_title} - {agenda_title}"

    return {
        "id": clip_id,
        "structData": {
            "content": content_payload,
            "meeting_title": meeting_title,
            "date": m_metadata.get("date"),
            "meeting_time": meeting_time,
            "sort_timestamp": sort_ts,
            "start_time": clip["start"],
            "end_time": clip["end"],
            "transcript_url": rel_path,
            "deep_link": deep_link,
            "jurisdiction": jurisdiction,
            "chamber": chamber,
            "committee": committee,
            "meeting_id": meeting_folder,
            "agenda_item": agenda_item,
            "meeting_path": f"{chamber}/{committee}/{meeting_folder}",
            "speakers": clip["speakers"],
        },
    }


def parse_meeting_path(path_str):
    """Extracts (chamber, committee, meeting_folder) from a relative transcript path."""
    parts = path_str.strip("/").split("/")
    if parts and parts[-1].endswith(".html"):
        parts = parts[:-1]

    chamber = parts[0] if len(parts) > 0 else "unknown"
    committee = "General"
    meeting_folder = "unknown"

    if len(parts) >= 3:
        chamber, committee, meeting_folder = parts[0], parts[1], parts[2]
    elif len(parts) == 2:
        if "-" in parts[1] and any(c.isdigit() for c in parts[1]):
            meeting_folder = parts[1]
            committee = "General"
        else:
            committee = parts[1]
            meeting_folder = "unknown"

    return chamber, committee, meeting_folder
