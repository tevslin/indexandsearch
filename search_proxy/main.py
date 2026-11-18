"""
search_proxy/main.py
Purpose: Google Cloud Function HTTP proxy for executing semantic search queries
against structured Vertex AI Search indices. Handles API key authentication,
dynamic relevance threshold tuning, POSIX epoch date range filtering, and
native bolded snippet extraction with metadata fallback.
BoM: Vertex AI Search HTTP Cloud Function Proxy.
"""
from datetime import datetime
import json
import os
import time
import functions_framework
from google.api_core.client_options import ClientOptions
from google.cloud import discoveryengine_v1beta as discoveryengine

# Configuration loaded from environment variables (no hardcoded keys)
PROJECT_ID = os.environ.get("PROJECT_ID") or os.environ.get("GCP_PROJECT_ID", "your-gcp-project-id")
API_KEY = os.environ.get("SEARCH_API_KEY", "")


@functions_framework.http
def search_transcripts(request):
    """HTTP Cloud Function entrypoint for semantic search across structured transcripts."""
    start_timer = time.time()

    if request.method == "OPTIONS":
        headers = {
            "Access-Control-Allow-Origin": "*",
            "Access-Control-Allow-Methods": "POST, GET, OPTIONS",
            "Access-Control-Allow-Headers": "Content-Type, X-API-Key",
            "Access-Control-Max-Age": "3600",
        }
        return ("", 204, headers)

    headers = {"Access-Control-Allow-Origin": "*"}

    provided_key = request.headers.get("X-API-Key") or request.args.get("key")
    if API_KEY and provided_key != API_KEY:
        return (json.dumps({"error": "Unauthorized"}), 401, headers)

    request_json = request.get_json(silent=True) or {}

    query = request.args.get("query") or request_json.get("query")
    jurisdiction = (
        request.args.get("jurisdiction")
        or request_json.get("jurisdiction")
        or "default"
    )
    max_results = request.args.get("max_results") or request_json.get("max_results") or 10
    sort = request.args.get("sort") or request_json.get("sort") or "relevance"

    index_name = request.args.get("index_name") or request_json.get("index_name")
    region = request.args.get("region") or request_json.get("region")

    if not index_name or not region:
        return (
            json.dumps(
                {"error": "Missing mandatory routing parameters: index_name and region"}
            ),
            400,
            headers,
        )

    if not query:
        return (json.dumps({"error": "Missing query parameter"}), 400, headers)

    committee_param = request.args.get("committee") or request_json.get("committee")
    meeting_path = request.args.get("meeting_path") or request_json.get("meeting_path")
    meeting_id = request.args.get("meeting_id") or request_json.get("meeting_id")
    chamber = request.args.get("chamber") or request_json.get("chamber")
    date_ge = request.args.get("date_ge") or request_json.get("date_ge")
    date_le = request.args.get("date_le") or request_json.get("date_le")
    relevance_threshold = (
        request.args.get("relevance_threshold")
        or request_json.get("relevance_threshold")
    )

    try:
        max_results = int(max_results)
        parsed_chamber, parsed_committee = parse_committee_filter(committee_param)

        filters = {
            "chamber": parsed_chamber or chamber,
            "committee": parsed_committee,
            "meeting_path": meeting_path,
            "meeting_id": meeting_id,
            "date_ge": date_ge,
            "date_le": date_le,
        }
        results = perform_search(
            query=query,
            jurisdiction=jurisdiction,
            max_results=max_results,
            sort=sort,
            filter_dict=filters,
            index_name=index_name,
            region=region,
            relevance_threshold=relevance_threshold,
        )

        latency_ms = round((time.time() - start_timer) * 1000, 2)
        log_payload = {
            "event_type": "TRANSCRIPT_SEARCH_LOG",
            "latency_ms": latency_ms,
            "request_params": {
                "query": query,
                "jurisdiction": jurisdiction,
                "index_name": index_name,
                "region": region,
                "sort": sort,
                "max_results": max_results,
                "filters": filters,
            },
            "results_count": len(results),
        }
        print(json.dumps(log_payload), flush=True)

        return (json.dumps({"results": results}), 200, headers)
    except ValueError as ve:
        return (json.dumps({"error": str(ve)}), 400, headers)
    except Exception as e:
        import traceback

        traceback.print_exc()
        return (json.dumps({"error": str(e)}), 500, headers)


def parse_committee_filter(committee_str):
    """Splits combined strings like 'Senate Transportation' into (chamber, committee)."""
    if not committee_str:
        return None, None

    for prefix in ("Senate ", "House "):
        if committee_str.strip().startswith(prefix):
            return prefix.strip(), committee_str[len(prefix) :].strip()

    return None, committee_str.strip()


def perform_search(
    query,
    jurisdiction,
    max_results,
    sort,
    filter_dict,
    index_name,
    region,
    relevance_threshold=None,
):
    """Constructs and executes a Vertex AI SearchRequest with snippet and threshold tuning."""
    api_endpoint = (
        f"{region}-discoveryengine.googleapis.com"
        if region != "global"
        else "discoveryengine.googleapis.com"
    )
    client_options = ClientOptions(api_endpoint=api_endpoint)
    client = discoveryengine.SearchServiceClient(client_options=client_options)

    order_by = ""
    if sort == "date_desc":
        order_by = "sort_timestamp desc"
    elif sort == "date_asc":
        order_by = "sort_timestamp"

    clean_project_id = PROJECT_ID.split()[0].strip(",")
    serving_config = (
        f"projects/{clean_project_id}/locations/{region}/collections/default_collection"
        f"/dataStores/{index_name}/servingConfigs/default_serving_config"
    )

    filter_parts = []
    if filter_dict.get("chamber"):
        filter_parts.append(f'chamber: ANY("{filter_dict["chamber"]}")')
    if filter_dict.get("committee"):
        filter_parts.append(f'committee: ANY("{filter_dict["committee"]}")')
    if filter_dict.get("meeting_path"):
        filter_parts.append(f'meeting_path: ANY("{filter_dict["meeting_path"]}")')
    if filter_dict.get("meeting_id"):
        filter_parts.append(f'meeting_id: ANY("{filter_dict["meeting_id"]}")')

    # Convert YYYY-MM-DD strings into POSIX timestamps against numeric 'sort_timestamp'
    if filter_dict.get("date_ge"):
        try:
            ge_epoch = datetime.strptime(filter_dict["date_ge"], "%Y-%m-%d").timestamp()
            filter_parts.append(f"sort_timestamp >= {ge_epoch}")
        except ValueError:
            raise ValueError("date_ge must be in YYYY-MM-DD format")

    if filter_dict.get("date_le"):
        try:
            le_epoch = (
                datetime.strptime(filter_dict["date_le"], "%Y-%m-%d").timestamp() + 86400
            )
            filter_parts.append(f"sort_timestamp <= {le_epoch}")
        except ValueError:
            raise ValueError("date_le must be in YYYY-MM-DD format")

    final_filter = " AND ".join(filter_parts) if filter_parts else ""

    kwargs = {
        "serving_config": serving_config,
        "query": query,
        "page_size": max_results,
        "order_by": order_by,
        "filter": final_filter,
        "content_search_spec": discoveryengine.SearchRequest.ContentSearchSpec(
            snippet_spec=discoveryengine.SearchRequest.ContentSearchSpec.SnippetSpec(
                return_snippet=True
            )
        ),
    }

    if relevance_threshold is not None and str(relevance_threshold).strip() != "":
        try:
            val = int(relevance_threshold)
            if val in (1, 2, 3, 4):
                kwargs["relevance_threshold"] = val
        except ValueError:
            pass

    # Dynamic Relevance Threshold default:
    # 1 (LOWEST) for chronological sorts to prevent date starvation;
    # 2 (LOW) for relevance sorts to filter out weak matches.
    if "relevance_threshold" not in kwargs:
        kwargs["relevance_threshold"] = 1 if order_by else 2

    search_request = discoveryengine.SearchRequest(**kwargs)
    response = client.search(search_request)

    results = []
    for result in response.results:
        doc = result.document
        struct_data = doc.struct_data

        snippet = ""
        if hasattr(result, "snippet") and result.snippet:
            snippet = result.snippet

        if not snippet and hasattr(doc, "derived_struct_data") and doc.derived_struct_data:
            derived = doc.derived_struct_data
            if "snippets" in derived and len(derived["snippets"]) > 0:
                snippet = derived["snippets"][0].get("snippet", "")
            elif "snippet" in derived:
                snippet = derived["snippet"]

        if snippet and "No snippet is available" in snippet:
            snippet = ""

        if not snippet:
            content = struct_data.get("content", "")
            snippet = (
                " ".join(content[:300].split()) + "..."
                if content
                else "Semantic match found in transcript segment."
            )

        transcript_url = struct_data.get("transcript_url", "")
        start_time = struct_data.get("start_time", 0)
        end_time = struct_data.get("end_time", 0)

        chamber_val = struct_data.get("chamber")
        committee_val = struct_data.get("committee")
        date_val = struct_data.get("date")
        title_val = struct_data.get("meeting_title")
        deep_link_val = struct_data.get("deep_link")

        if not chamber_val and transcript_url:
            parts = transcript_url.strip("/").split("/")
            if len(parts) >= 4 and parts[0] == "meetings":
                chamber_val = parts[1]
                committee_val = parts[2].replace("_", " ") if parts[2] != "General" else ""
                folder = parts[3]
                date_val = folder.split("_")[0] if "_" in folder else folder

        if not title_val:
            chamber_prefix = f"{chamber_val} " if chamber_val else ""
            committee_prefix = f"{committee_val} " if committee_val else ""
            title_val = f"{chamber_prefix}{committee_prefix}- {date_val}"

        if not deep_link_val and transcript_url:
            deep_link_val = f"{transcript_url}?startTime={start_time}&endTime={end_time}"

        results.append(
            {
                "id": doc.id,
                "text": struct_data.get("content", ""),
                "snippet": snippet,
                "meeting_title": title_val,
                "chamber": chamber_val,
                "committee": committee_val,
                "date": date_val,
                "meeting_time": struct_data.get("meeting_time"),
                "meeting_id": struct_data.get("meeting_id"),
                "meeting_path": struct_data.get("meeting_path"),
                "start_time": start_time,
                "end_time": end_time,
                "timestamp": start_time,
                "deep_link": deep_link_val,
                "transcript_url": transcript_url,
                "jurisdiction": struct_data.get("jurisdiction"),
                "agenda_item": struct_data.get("agenda_item"),
            }
        )

    return results
