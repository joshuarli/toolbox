import argparse
import hashlib
import html
import json
import os
import subprocess
import urllib.parse
import urllib.request

from tool.movies.paths import data_dir

MOVIE_EXTENSIONS = {".mkv", ".mp4", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v"}
TMDB = "ae24337f19d0166cfd8ba973aad4b455"


def _poster_dir():
    return data_dir() / "movie-library-data"


def get_movie_metadata(query):
    poster_dir = _poster_dir()
    if not os.path.exists(poster_dir):
        os.makedirs(poster_dir)

    clean_name = os.path.splitext(query)[0]
    name_hash = hashlib.md5(clean_name.encode()).hexdigest()
    meta_path = os.path.join(poster_dir, f"{name_hash}.json")
    poster_path = os.path.join(poster_dir, f"{name_hash}.jpg")

    # Try to load cached metadata
    if os.path.exists(meta_path):
        with open(meta_path, encoding="utf-8") as f:
            return json.load(f)

    try:
        # 1. Search for the movie
        search_url = f"https://api.themoviedb.org/3/search/movie?api_key={TMDB}&query={urllib.parse.quote(clean_name)}"
        with urllib.request.urlopen(search_url) as response:
            search_data = json.loads(response.read().decode())
            if not search_data.get("results"):
                print(f"No results found for: {clean_name}")
                return None

            movie_id = search_data["results"][0]["id"]

            # 2. Get full movie details including credits (for director)
            detail_url = f"https://api.themoviedb.org/3/movie/{movie_id}?api_key={TMDB}&append_to_response=credits"
            with urllib.request.urlopen(detail_url) as detail_resp:
                full_data = json.loads(detail_resp.read().decode())

                # Extract director
                director = "Unknown"
                crew = full_data.get("credits", {}).get("crew", [])
                for person in crew:
                    if person.get("job") == "Director":
                        director = person.get("name", "Unknown")
                        break

                # Extract year
                release_date = full_data.get("release_date", "")
                year = release_date.split("-")[0] if release_date else "Unknown"

                # Handle poster
                poster_path_tmdb = full_data.get("poster_path")
                if poster_path_tmdb:
                    img_url = f"https://image.tmdb.org/t/p/w200{poster_path_tmdb}"
                    with urllib.request.urlopen(img_url) as img_resp, open(poster_path, "wb") as f:
                        f.write(img_resp.read())

                meta = {
                    "director": director,
                    "year": year,
                    "poster": poster_path if os.path.exists(poster_path) else None,
                    "title": full_data.get("title", clean_name),
                }

                # Cache the processed metadata
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f)

                return meta
    except Exception as e:
        print(f"Error fetching metadata for {clean_name}: {e}")

    return None


def format_size(size_bytes):
    if size_bytes == 0:
        return "0B"
    size_bytes = float(size_bytes)
    units = ("B", "K", "M", "G", "T", "P")
    import math

    i = math.floor(math.log(size_bytes, 1024))
    p = math.pow(1024, i)
    s = round(size_bytes / p, 2)
    return f"{s}{units[i]}"


def get_movie_info(filepath):
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", filepath],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="ignore",
        )
        data = json.loads(result.stdout)
    except FileNotFoundError:
        return None, "ffprobe not found"
    except json.JSONDecodeError:
        return None, "Failed to parse ffprobe output"
    except Exception as e:
        return None, str(e)

    fmt = data.get("format", {})
    streams = data.get("streams", [])
    raw_size = os.path.getsize(filepath)
    filename = os.path.basename(filepath)

    # Get movie metadata (poster, director, year)
    meta = get_movie_metadata(filename) or {}

    info = {
        "filename": filename,
        "size": raw_size,
        "size_pretty": format_size(raw_size),
        "duration": 0,
        "duration_pretty": "Unknown",
        "bitrate": 0,
        "bitrate_pretty": "Unknown",
        "video": {"codec": "Unknown", "encoder": "Unknown", "resolution": "Unknown"},
        "audio": {"codec": "Unknown", "bitrate": "Unknown"},
        "poster": meta.get("poster"),
        "director": meta.get("director", "Unknown"),
        "year": meta.get("year", "Unknown"),
    }

    # Format info
    duration_sec = fmt.get("duration")
    if duration_sec:
        try:
            seconds = float(duration_sec)
            info["duration"] = seconds
            hours = int(seconds // 3600)
            minutes = int((seconds % 3600) // 60)
            secs = int(seconds % 60)
            info["duration_pretty"] = f"{hours:02}:{minutes:02}:{secs:02}"
        except ValueError:
            pass

    bitrate = fmt.get("bit_rate")
    if bitrate:
        try:
            b_rate = int(bitrate)
            info["bitrate"] = b_rate
            info["bitrate_pretty"] = f"{b_rate // 1000} kb/s"
        except ValueError:
            pass

    # Stream info
    for s in streams:
        if s.get("codec_type") == "video" and info["video"]["codec"] == "Unknown":
            codec = s.get("codec_name", "Unknown")
            encoder = s.get("metadata", {}).get("encoder", "Unknown")
            info["video"]["codec"] = codec
            info["video"]["encoder"] = encoder
            w = s.get("width")
            h = s.get("height")
            if w and h:
                info["video"]["resolution"] = f"{w}x{h}"

        elif s.get("codec_type") == "audio" and info["audio"]["codec"] == "Unknown":
            codec = s.get("codec_name", "Unknown")
            if codec == "Unknown":
                codec = s.get("metadata", {}).get("codec", "Unknown")
            info["audio"]["codec"] = codec
            a_bitrate = s.get("bit_rate")
            if a_bitrate:
                info["audio"]["bitrate"] = f"{int(a_bitrate) // 1000} kb/s"

    return info, None


def generate_html(movies):
    css = """
    :root {
        --bg: #f8f9fa;
        --text: #212529;
        --accent: #4a90e2;
        --card-bg: #ffffff;
        --border: #e9ecef;
    }
    body {
        font-family: 'Inter', -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
        background-color: var(--bg);
        color: var(--text);
        margin: 0;
        padding: 40px 20px;
        display: flex;
        justify-content: center;
        line-height: 1.5;
    }
    .container {
        max-width: 1300px;
        width: 100%;
    }
    h1 {
        font-weight: 800;
        font-size: 2.25rem;
        margin-bottom: 1rem;
        text-align: center;
        letter-spacing: -0.025em;
        color: #111;
    }
    .filter-bar {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-bottom: 20px;
        justify-content: center;
    }
    .pill {
        display: inline-flex;
        align-items: center;
        padding: 4px 12px;
        background: var(--accent);
        color: white;
        border-radius: 20px;
        font-size: 0.8rem;
        font-weight: 500;
        cursor: pointer;
        transition: opacity 0.2s;
    }
    .pill:hover { opacity: 0.8; }
    .pill .remove {
        margin-left: 6px;
        font-weight: bold;
        opacity: 0.7;
    }
    table {
        width: 100%;
        border-collapse: separate;
        border-spacing: 0;
        background: var(--card-bg);
        border-radius: 12px;
        overflow: hidden;
        box-shadow: 0 10px 30px rgba(0,0,0,0.04);
        border: 1px solid var(--border);
    }
    th {
        text-align: left;
        background-color: #fdfdfd;
        color: #6c757d;
        font-weight: 600;
        font-size: 0.75rem;
        text-transform: uppercase;
        padding: 16px 24px;
        border-bottom: 1px solid var(--border);
        letter-spacing: 0.05em;
        cursor: pointer;
        user-select: none;
        position: relative;
    }
    th:hover { background-color: #f5f5f5; }
    th::after {
        content: '↕';
        position: absolute;
        right: 10px;
        opacity: 0.3;
    }
    td {
        padding: 16px 24px;
        border-bottom: 1px solid var(--border);
        font-size: 0.9rem;
    }
    tr:last-child td {
        border-bottom: none;
    }
    tr:hover {
        background-color: #fcfcfc;
    }
    .poster {
        width: 50px;
        height: 75px;
        object-fit: cover;
        border-radius: 4px;
        background: #eee;
        box-shadow: 0 2px 5px rgba(0,0,0,0.1);
    }
    .badge {
        display: inline-block;
        padding: 3px 8px;
        border-radius: 6px;
        font-size: 0.75rem;
        font-weight: 500;
        background: #f1f3f5;
        color: #495057;
        cursor: pointer;
        transition: background 0.2s;
    }
    .badge:hover { background: #e9ecef; }
    .res { background: #e7f5ff; color: #1971c2; }
    .codec { background: #f3f0ff; color: #6741d9; }
    .duration { font-variant-numeric: tabular-nums; color: #868e96; }
    """

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Movie Library</title>
    <style>{css}</style>
</head>
<body>
    <div class="container">
        <h1>Movie Library</h1>
        <div class="filter-bar" id="filterBar"></div>
        <table id="movieTable">
            <thead>
                <tr>
                    <th>Poster</th>
                    <th onclick="sortTable(1)">File Name</th>
                    <th onclick="sortTable(2)">Year</th>
                    <th onclick="sortTable(3)">Director</th>
                    <th onclick="sortTable(4)">Size</th>
                    <th onclick="sortTable(5)">Video Quality</th>
                    <th onclick="sortTable(6)">Audio Codec</th>
                    <th onclick="sortTable(7)">Duration</th>
                </tr>
            </thead>
            <tbody>
    """

    for m in movies:
        video_info = (
            f"{m['video']['codec']} ({m['video']['encoder']})"
            if m["video"]["encoder"] != "Unknown"
            else m["video"]["codec"]
        )
        poster_html = (
            f'<img src="{m["poster"]}" class="poster">'
            if m["poster"]
            else '<div class="poster" style="display:flex;align-items:center;justify-content:center;font-size:10px;color:#aaa;text-align:center">N/A</div>'
        )

        video_details = f"""
            <span class="badge res" onclick="addFilter(5, this.innerText)">{m["video"]["resolution"]}</span>
            <span class="badge codec" onclick="addFilter(5, this.innerText)">{video_info}</span>
            <span class="badge" onclick="addFilter(5, this.innerText)">{m["bitrate_pretty"]}</span>
        """

        row = f"""
                <tr>
                    <td>{poster_html}</td>
                    <td style="font-weight: 500;">{html.escape(m["filename"])}</td>
                    <td>{m["year"]}</td>
                    <td><span class="badge" onclick="addFilter(3, this.innerText)">{m["director"]}</span></td>
                    <td data-sort="{m["size"]}">{m["size_pretty"]}</td>
                    <td data-sort="{m["bitrate"]}">{video_details}</td>
                    <td><span class="badge codec" onclick="addFilter(6, this.innerText)">{m["audio"]["codec"]} {f"({m['audio']['bitrate']})" if m["audio"]["bitrate"] != "Unknown" else ""}</span></td>
                    <td data-sort="{m["duration"]}" class="duration">{m["duration_pretty"]}</td>
                </tr>
        """
        html_content += row

    html_content += """
            </tbody>
        </table>
    </div>
    <script>
        let filters = {};
        let sortCol = -1;
        let sortAsc = true;

        function addFilter(colIndex, value) {
            filters[colIndex] = value;
            renderFilters();
            applyFilters();
        }

        function removeFilter(colIndex) {
            delete filters[colIndex];
            renderFilters();
            applyFilters();
        }

        function renderFilters() {
            const bar = document.getElementById('filterBar');
            bar.innerHTML = '';
            for (let col in filters) {
                const pill = document.createElement('div');
                pill.className = 'pill';
                pill.innerHTML = `${filters[col]} <span class="remove" onclick="event.stopPropagation(); removeFilter(${col})">✕</span>`;
                bar.appendChild(pill);
            }
        }

        function applyFilters() {
            const rows = document.querySelectorAll('#movieTable tbody tr');
            rows.forEach(row => {
                let visible = true;
                for (let col in filters) {
                    const cellText = row.cells[col].innerText;
                    if (!cellText.includes(filters[col])) {
                        visible = false;
                        break;
                    }
                }
                row.style.display = visible ? '' : 'none';
            });
        }

        function sortTable(n) {
            const table = document.getElementById("movieTable");
            const tbody = table.tBodies[0];
            const rows = Array.from(tbody.rows);

            sortAsc = (sortCol === n) ? !sortAsc : true;
            sortCol = n;

            rows.sort((a, b) => {
                const cellA = a.cells[n];
                const cellB = b.cells[n];

                let valA = cellA.dataset.sort || cellA.innerText.toLowerCase();
                let valB = cellB.dataset.sort || cellB.innerText.toLowerCase();

                const numA = parseFloat(valA);
                const numB = parseFloat(valB);

                if (!isNaN(numA) && !isNaN(numB)) {
                    return sortAsc ? numA - numB : numB - numA;
                }

                return sortAsc ? valA.localeCompare(valB) : valB.localeCompare(valA);
            });

            rows.forEach(row => tbody.appendChild(row));
        }
    </script>
</body>
</html>
    """
    return html_content


def run_generate(directory):
    movies = []
    files = [f"{directory}/{f}" for f in os.listdir(directory) if os.path.splitext(f)[1].lower() in MOVIE_EXTENSIONS]

    if not files:
        print(f"No movie files found in {directory}")
        return

    print(f"Found {len(files)} movie files. Processing...")

    for f in files:
        print(f"Processing {f}...", end="\r")
        info, err = get_movie_info(f)
        if info:
            movies.append(info)
        else:
            print(f"\nError processing {f}: {err}")

    print("\nGenerating library.html...")
    with open("library.html", "w", encoding="utf-8") as f:
        f.write(generate_html(movies))

    print("Done! Open library.html to see your library.")


def run_fetch(query):
    print(f"Fetching metadata for: {query}...")
    meta = get_movie_metadata(query)
    if meta:
        print(f"Successfully fetched metadata for: {meta.get('title', query)}")
        print(f"Director: {meta.get('director')}")
        print(f"Year: {meta.get('year')}")
        print(f"Poster cached at: {meta.get('poster')}")
    else:
        print("Failed to fetch metadata.")


def main():
    parser = argparse.ArgumentParser(description="Movie Library Manager")
    subparsers = parser.add_subparsers(dest="command", required=True)

    parser_gen = subparsers.add_parser("generate", help="Generate the HTML movie library page")
    parser_gen.add_argument("dir", type=str, help="media directory (flat)")

    parser_fetch = subparsers.add_parser("fetch", help="Fetch metadata for a specific movie")
    parser_fetch.add_argument("query", type=str, help="Movie title to search for")

    args = parser.parse_args()

    if args.command == "generate":
        run_generate(args.dir)
    elif args.command == "fetch":
        run_fetch(args.query)


if __name__ == "__main__":
    main()
