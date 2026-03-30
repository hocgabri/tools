#!/usr/bin/env python3
"""
Duplex scanning tool for simplex-only scanners (e.g. Canon TR7100).

Uses the eSCL (AirScan) protocol to talk directly to the scanner over
HTTP — no SANE or platform-specific drivers needed.

Workflow:
  1. Place documents face-up in the ADF (page 1 on top).
  2. The tool scans all front sides via the document feeder.
  3. You are prompted to flip the entire output stack and re-load it.
  4. The tool scans all back sides.
  5. Asked if there are more pages — if yes, repeat from step 1.
  6. All batches are merged into a single PDF.

Requirements:
  - Python packages: img2pdf, Pillow  (pip install -r requirements.txt)
  - Scanner must support eSCL (AirScan) — most modern network scanners do

Usage:
  python3 duplex_scan.py [options]

Examples:
  python3 duplex_scan.py                          # auto-discover scanner
  python3 duplex_scan.py -o my_document.pdf       # custom output name
  python3 duplex_scan.py --dpi 200 --mode Gray    # 200 dpi grayscale
  python3 duplex_scan.py --host 192.168.86.21     # specify scanner IP
  python3 duplex_scan.py --fronts-only            # scan only front sides
"""

import argparse
import glob
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from xml.etree import ElementTree


ESCL_NS = {
    "scan": "http://schemas.hp.com/imaging/escl/2011/05/03",
    "pwg": "http://www.pwg.org/schemas/2010/12/sm",
}

# Paper sizes in 300ths of an inch
PAPER_SIZES = {
    "a4": (2480, 3507),
    "letter": (2550, 3300),
    "legal": (2550, 4200),
}

COLOR_MODES = {
    "Color": "RGB24",
    "Gray": "Grayscale8",
}


def check_dependencies():
    """Verify required Python packages are available."""
    try:
        import img2pdf  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("ERROR: Missing Python packages. Install them with:")
        print("  pip install img2pdf Pillow")
        sys.exit(1)


def discover_scanner():
    """Auto-discover eSCL scanners on the local network."""
    system = platform.system()

    if system == "Darwin":
        return _discover_macos()
    else:
        return _discover_linux()


def _discover_macos():
    """Discover scanners using macOS dns-sd."""
    print("Discovering scanners on the network...")

    # Browse for eSCL scanners
    try:
        proc = subprocess.run(
            ["dns-sd", "-B", "_uscan._tcp", "."],
            capture_output=True, text=True, timeout=5,
        )
    except subprocess.TimeoutExpired:
        pass
    except FileNotFoundError:
        print("ERROR: dns-sd not found (should be built into macOS).")
        sys.exit(1)

    # Use dns-sd -Z for a single-shot listing that includes all info
    # Fall back to a targeted approach: browse, then resolve
    try:
        proc = subprocess.run(
            ["dns-sd", "-B", "_uscan._tcp", "."],
            capture_output=True, text=True, timeout=4,
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        output = (e.stdout or "") + (e.stderr or "") if hasattr(e, "stdout") else ""

    # Parse instance names from browse output
    instances = []
    for line in output.splitlines():
        # Lines like: 14:42:49.875  Add  2  11 local.  _uscan._tcp.  Canon TR7100 series
        parts = line.split()
        if len(parts) >= 7 and "Add" in parts:
            # Instance name is everything after the service type column
            try:
                idx = next(
                    i for i, p in enumerate(parts) if "_uscan._tcp." in p
                )
                name = " ".join(parts[idx + 1 :])
                if name and name not in instances:
                    instances.append(name)
            except StopIteration:
                continue

    if not instances:
        return None

    # Resolve the first (or chosen) instance
    instance = instances[0]
    if len(instances) > 1:
        print("Multiple scanners found:")
        for i, name in enumerate(instances, 1):
            print(f"  {i}. {name}")
        while True:
            choice = input(f"Select scanner [1-{len(instances)}]: ").strip()
            try:
                idx = int(choice) - 1
                if 0 <= idx < len(instances):
                    instance = instances[idx]
                    break
            except ValueError:
                pass
            print("Invalid choice, try again.")

    print(f"Found: {instance}")

    # Resolve hostname
    try:
        proc = subprocess.run(
            ["dns-sd", "-L", instance, "_uscan._tcp", "."],
            capture_output=True, text=True, timeout=5,
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        output = ""
        if hasattr(e, "stdout") and e.stdout:
            output += e.stdout
        if hasattr(e, "stderr") and e.stderr:
            output += e.stderr

    # Parse hostname from: "can be reached at hostname.local.:port"
    hostname = None
    port = 80
    for line in output.splitlines():
        if "can be reached at" in line:
            part = line.split("can be reached at")[1].strip()
            # Format: hostname.local.:port (...)
            hp = part.split()[0]  # "hostname.local.:80"
            if ":" in hp:
                hostname = hp.rsplit(":", 1)[0]
                try:
                    port = int(hp.rsplit(":", 1)[1])
                except ValueError:
                    port = 80
            else:
                hostname = hp
            break

    if not hostname:
        return None

    # Resolve to IP
    try:
        proc = subprocess.run(
            ["dns-sd", "-G", "v4", hostname],
            capture_output=True, text=True, timeout=5,
        )
        output = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as e:
        output = ""
        if hasattr(e, "stdout") and e.stdout:
            output += e.stdout
        if hasattr(e, "stderr") and e.stderr:
            output += e.stderr

    for line in output.splitlines():
        parts = line.split()
        if "Add" in parts:
            for part in parts:
                # Match an IP address pattern
                if part.count(".") == 3:
                    try:
                        octets = part.split(".")
                        if all(0 <= int(o) <= 255 for o in octets):
                            print(f"Scanner IP: {part}")
                            return f"http://{part}:{port}"
                    except ValueError:
                        continue

    return None


def _discover_linux():
    """Discover scanners using avahi-browse on Linux."""
    print("Discovering scanners on the network...")

    if not shutil.which("avahi-browse"):
        print("Note: avahi-browse not found. Install avahi-utils for auto-discovery,")
        print("      or specify the scanner IP with --host.")
        return None

    try:
        proc = subprocess.run(
            ["avahi-browse", "-rt", "_uscan._tcp"],
            capture_output=True, text=True, timeout=10,
        )
        output = proc.stdout
    except subprocess.TimeoutExpired:
        return None

    # Parse avahi-browse resolve output for address and port
    address = None
    port = 80
    for line in output.splitlines():
        line = line.strip()
        if line.startswith("address = ["):
            addr = line.split("[")[1].split("]")[0]
            # Prefer IPv4
            if "." in addr:
                address = addr
        elif line.startswith("port = ["):
            try:
                port = int(line.split("[")[1].split("]")[0])
            except ValueError:
                pass

    if address:
        print(f"Found scanner at {address}:{port}")
        return f"http://{address}:{port}"

    return None


def get_scanner_capabilities(base_url):
    """Fetch and parse eSCL scanner capabilities."""
    url = f"{base_url}/eSCL/ScannerCapabilities"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DuplexScan/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except urllib.error.URLError as e:
        print(f"ERROR: Cannot reach scanner at {base_url}")
        print(f"  {e}")
        sys.exit(1)

    root = ElementTree.fromstring(xml_data)

    caps = {
        "has_adf": False,
        "has_platen": False,
        "resolutions": [],
        "color_modes": [],
    }

    # Check for ADF and Platen support
    for source in root.iter():
        tag = source.tag.split("}")[-1] if "}" in source.tag else source.tag
        if tag == "Adf":
            caps["has_adf"] = True
        elif tag == "Platen":
            caps["has_platen"] = True

    return caps


def get_scanner_status(base_url):
    """Check if the scanner is idle and ready."""
    url = f"{base_url}/eSCL/ScannerStatus"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "DuplexScan/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            xml_data = resp.read()
    except urllib.error.URLError:
        return "Unknown"

    root = ElementTree.fromstring(xml_data)
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "State":
            return elem.text
    return "Unknown"


def create_scan_job(base_url, dpi, mode, source, paper_size):
    """Submit an eSCL scan job and return the job URL."""
    width, height = PAPER_SIZES.get(paper_size.lower(), PAPER_SIZES["a4"])

    # Scale dimensions to match requested DPI (sizes are defined at 300 DPI)
    scale = dpi / 300
    width = int(width * scale)
    height = int(height * scale)

    color_mode = COLOR_MODES.get(mode, "RGB24")
    input_source = "Feeder" if source == "adf" else "Platen"

    xml_body = f"""<?xml version="1.0" encoding="UTF-8"?>
<scan:ScanSettings xmlns:scan="http://schemas.hp.com/imaging/escl/2011/05/03"
                   xmlns:pwg="http://www.pwg.org/schemas/2010/12/sm">
  <pwg:Version>2.0</pwg:Version>
  <scan:Intent>Document</scan:Intent>
  <pwg:ScanRegions>
    <pwg:ScanRegion>
      <pwg:ContentRegionUnits>escl:ThreeHundredthsOfInches</pwg:ContentRegionUnits>
      <pwg:Height>{height}</pwg:Height>
      <pwg:Width>{width}</pwg:Width>
      <pwg:XOffset>0</pwg:XOffset>
      <pwg:YOffset>0</pwg:YOffset>
    </pwg:ScanRegion>
  </pwg:ScanRegions>
  <pwg:InputSource>{input_source}</pwg:InputSource>
  <scan:ColorMode>{color_mode}</scan:ColorMode>
  <scan:XResolution>{dpi}</scan:XResolution>
  <scan:YResolution>{dpi}</scan:YResolution>
  <pwg:DocumentFormat>image/jpeg</pwg:DocumentFormat>
</scan:ScanSettings>"""

    url = f"{base_url}/eSCL/ScanJobs"
    data = xml_body.encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={
            "Content-Type": "text/xml",
            "User-Agent": "DuplexScan/1.0",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            # 201 Created — Location header has job URL
            location = resp.headers.get("Location", "")
            if location:
                # Location may be relative or absolute
                if location.startswith("http"):
                    return location
                return f"{base_url}{location}"
            # Some scanners return the job URL in the response body
            body = resp.read().decode("utf-8", errors="replace")
            if "/eSCL/ScanJobs/" in body:
                return body.strip()
    except urllib.error.HTTPError as e:
        if e.code == 409:
            print("ERROR: Scanner is busy. Wait for the current job to finish.")
        else:
            print(f"ERROR: Failed to create scan job (HTTP {e.code})")
            print(f"  {e.read().decode('utf-8', errors='replace')[:500]}")
        sys.exit(1)
    except urllib.error.URLError as e:
        print(f"ERROR: Cannot connect to scanner: {e}")
        sys.exit(1)

    print("ERROR: Scanner did not return a job URL.")
    sys.exit(1)


def fetch_scanned_pages(job_url, output_dir, prefix):
    """Fetch all scanned pages from an eSCL scan job."""
    files = []
    page_num = 0

    while True:
        page_num += 1
        url = f"{job_url}/NextDocument"

        try:
            req = urllib.request.Request(url, headers={"User-Agent": "DuplexScan/1.0"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                content_type = resp.headers.get("Content-Type", "image/jpeg")
                ext = "jpg" if "jpeg" in content_type else "png"
                filename = os.path.join(output_dir, f"{prefix}_{page_num:03d}.{ext}")

                with open(filename, "wb") as f:
                    while True:
                        chunk = resp.read(65536)
                        if not chunk:
                            break
                        f.write(chunk)

                files.append(filename)
                print(f"  Page {page_num} received.")

        except urllib.error.HTTPError as e:
            if e.code == 404:
                # No more pages — ADF is empty
                break
            elif e.code == 503:
                # Scanner busy, retry after a short wait
                time.sleep(1)
                page_num -= 1
                continue
            else:
                print(f"  Stopped at page {page_num} (HTTP {e.code})")
                break
        except urllib.error.URLError as e:
            print(f"  Connection lost at page {page_num}: {e}")
            break

    return files


def scan_batch(base_url, output_dir, prefix, dpi, mode, source, paper_size):
    """Scan a batch of pages from the ADF via eSCL."""
    print(f"\nScanning {prefix} pages...")

    # Check scanner status
    status = get_scanner_status(base_url)
    if status not in ("Idle", "Unknown"):
        print(f"WARNING: Scanner status is '{status}', attempting scan anyway...")

    # Create scan job
    print("  Creating scan job...")
    job_url = create_scan_job(base_url, dpi, mode, source, paper_size)
    print(f"  Job URL: {job_url}")

    # Fetch pages
    print("  Fetching scanned pages from ADF...\n")
    files = fetch_scanned_pages(job_url, output_dir, prefix)

    print(f"\n  Scanned {len(files)} page(s).")
    return files


def interleave_pages(fronts, backs):
    """
    Interleave front and back page files into the correct order.

    Fronts come in order: page 1, 3, 5, ...
    Backs come in REVERSE order from the ADF: last-back first.
    So backs need to be reversed to get: page 2, 4, 6, ...

    Result: page 1, page 2, page 3, page 4, ...
    """
    backs_reversed = list(reversed(backs))

    pages = []
    for i in range(max(len(fronts), len(backs_reversed))):
        if i < len(fronts):
            pages.append(fronts[i])
        if i < len(backs_reversed):
            pages.append(backs_reversed[i])

    return pages


def create_pdf(image_files, output_path):
    """Combine scanned images into a single PDF."""
    import img2pdf
    from PIL import Image

    if not image_files:
        print("ERROR: No images to combine.")
        sys.exit(1)

    # Convert RGBA images to RGB for PDF compatibility
    prepared_files = []
    for img_path in image_files:
        with Image.open(img_path) as img:
            if img.mode in ("RGBA", "LA", "PA"):
                img = img.convert("RGB")
                img.save(img_path)
            prepared_files.append(img_path)

    print(f"\nCreating PDF with {len(prepared_files)} page(s)...")

    with open(output_path, "wb") as f:
        f.write(img2pdf.convert(prepared_files))

    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"Saved: {output_path} ({size_mb:.1f} MB)")


def main():
    parser = argparse.ArgumentParser(
        description="Duplex scanning for simplex-only scanners (Canon TR7100 etc.)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Scanning workflow:
  1. Load documents face-up in the ADF (page 1 on top)
  2. Front sides are scanned automatically
  3. Take the output stack, flip it upside-down, and reload into the ADF
  4. Back sides are scanned automatically
  5. Asked if there are more pages — if yes, repeat from step 1
  6. All batches are merged into a single PDF

Tips:
  - Make sure the ADF pickup roller is clean for reliable feeding
  - Do a test run with 2-3 pages first to verify page order
  - Use --fronts-only if you only need single-sided scanning via ADF
        """,
    )
    parser.add_argument(
        "-o", "--output", default=None,
        help="Output PDF filename (default: scan_YYYYMMDD_HHMMSS.pdf)",
    )
    parser.add_argument(
        "--dpi", type=int, default=300,
        help="Scan resolution in DPI (default: 300)",
    )
    parser.add_argument(
        "--mode", default="Color", choices=["Color", "Gray"],
        help="Scan mode (default: Color)",
    )
    parser.add_argument(
        "--host", default=None,
        help="Scanner IP address or hostname (auto-discovered if omitted)",
    )
    parser.add_argument(
        "--port", type=int, default=80,
        help="Scanner eSCL port (default: 80)",
    )
    parser.add_argument(
        "--paper", default="A4", choices=["A4", "Letter", "Legal"],
        help="Paper size (default: A4)",
    )
    parser.add_argument(
        "--fronts-only", action="store_true",
        help="Scan only front sides (skip duplex workflow)",
    )
    parser.add_argument(
        "--keep-images", action="store_true",
        help="Keep individual scanned images after creating PDF",
    )

    args = parser.parse_args()

    check_dependencies()

    # Determine scanner URL
    if args.host:
        base_url = f"http://{args.host}:{args.port}"
    else:
        base_url = discover_scanner()
        if not base_url:
            print("ERROR: No eSCL scanner found on the network.")
            print("  Specify the scanner IP manually with --host 192.168.x.x")
            sys.exit(1)

    # Verify scanner is reachable and has ADF
    print(f"\nConnecting to scanner at {base_url}...")
    caps = get_scanner_capabilities(base_url)

    if caps["has_adf"]:
        print("ADF: supported")
    else:
        print("WARNING: ADF not reported in capabilities. Will attempt anyway.")

    status = get_scanner_status(base_url)
    print(f"Status: {status}")

    # Create temp directory for scanned images
    work_dir = tempfile.mkdtemp(prefix="duplex_scan_")
    print(f"Working directory: {work_dir}")

    try:
        all_pages = []
        batch_num = 0

        while True:
            batch_num += 1
            batch_prefix = f"b{batch_num}"

            # --- Scan front sides ---
            print("\n" + "=" * 60)
            if batch_num == 1:
                print("STEP 1: SCANNING FRONT SIDES")
            else:
                print(f"BATCH {batch_num}: SCANNING FRONT SIDES")
            print("=" * 60)
            print("Load your documents FACE-UP in the ADF, page 1 on top.")
            input("Press ENTER when ready to scan front sides...")

            fronts = scan_batch(
                base_url, work_dir, f"{batch_prefix}_front",
                args.dpi, args.mode, "adf", args.paper,
            )

            if not fronts:
                if batch_num == 1:
                    print("ERROR: No front pages were scanned. Check your scanner and ADF.")
                    sys.exit(1)
                else:
                    print("No pages scanned in this batch, skipping.")
                    break

            # --- Ask whether to scan back sides ---
            scan_backs = False
            if args.fronts_only:
                all_pages.extend(fronts)
            else:
                print(f"\n{len(fronts)} front page(s) scanned successfully.")
                do_backs = input("Scan back sides for this batch? [Y/n]: ").strip().lower()
                if do_backs in ("n", "no"):
                    all_pages.extend(fronts)
                else:
                    scan_backs = True

            if scan_backs:
                # --- Scan back sides ---
                print("\n" + "=" * 60)
                if batch_num == 1:
                    print("STEP 2: SCANNING BACK SIDES")
                else:
                    print(f"BATCH {batch_num}: SCANNING BACK SIDES")
                print("=" * 60)
                print()
                print("Flip the pages for back-side scanning:")
                print("  1. Take the entire output stack from the scanner tray")
                print("  2. Flip the stack upside-down (do NOT reverse the order)")
                print("  3. Load the flipped stack back into the ADF")
                print()
                print("This means the LAST page's back will be scanned first,")
                print("and the tool will re-order them automatically.")
                input("\nPress ENTER when ready to scan back sides...")

                backs = scan_batch(
                    base_url, work_dir, f"{batch_prefix}_back",
                    args.dpi, args.mode, "adf", args.paper,
                )

                if not backs:
                    print("WARNING: No back pages scanned. Using fronts only for this batch.")
                    all_pages.extend(fronts)
                elif len(backs) != len(fronts):
                    print(f"WARNING: Front pages ({len(fronts)}) != back pages ({len(backs)}).")
                    print("         Pages will still be interleaved as best as possible.")
                    all_pages.extend(interleave_pages(fronts, backs))
                else:
                    all_pages.extend(interleave_pages(fronts, backs))

            # --- Ask if there are more pages ---
            total_so_far = len(all_pages)
            print(f"\n{total_so_far} total page(s) scanned so far.")
            more = input("More pages to scan? [y/N]: ").strip().lower()
            if more not in ("y", "yes"):
                break

        # --- Create final PDF ---
        output = args.output or f"scan_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        create_pdf(all_pages, output)
        print(f"\nDone! Scan saved to: {output}")
        print(f"Total pages in PDF: {len(all_pages)}")

    finally:
        if not args.keep_images:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"\nScanned images kept in: {work_dir}")


if __name__ == "__main__":
    main()
