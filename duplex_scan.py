#!/usr/bin/env python3
"""
Duplex scanning tool for simplex-only scanners (e.g. Canon TR7100).

Workflow:
  1. Place documents face-up in the ADF (page 1 on top).
  2. The tool scans all front sides via the document feeder.
  3. You are prompted to flip the entire output stack and re-load it.
  4. The tool scans all back sides.
  5. Front and back pages are interleaved into a single PDF.

Requirements:
  - SANE (scanimage) installed and your scanner detected
  - Python packages: img2pdf, Pillow  (pip install -r requirements.txt)

Usage:
  python3 duplex_scan.py [options]

Examples:
  python3 duplex_scan.py                          # defaults: 300 dpi, color, A4
  python3 duplex_scan.py -o my_document.pdf       # custom output name
  python3 duplex_scan.py --dpi 200 --mode Gray    # 200 dpi grayscale
  python3 duplex_scan.py --device \"pixma:...\"      # specify scanner device
  python3 duplex_scan.py --fronts-only             # scan only front sides
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from pathlib import Path


def check_dependencies():
    """Verify that scanimage and required Python packages are available."""
    if not shutil.which("scanimage"):
        print("ERROR: 'scanimage' not found. Install SANE:")
        print("  sudo apt install sane sane-utils")
        sys.exit(1)
    try:
        import img2pdf  # noqa: F401
        from PIL import Image  # noqa: F401
    except ImportError:
        print("ERROR: Missing Python packages. Install them with:")
        print("  pip install img2pdf Pillow")
        sys.exit(1)


def detect_scanner(preferred_device=None):
    """Detect available scanner, optionally preferring a specific device."""
    if preferred_device:
        return preferred_device

    print("Detecting scanners...")
    result = subprocess.run(
        ["scanimage", "-L"], capture_output=True, text=True, timeout=30
    )
    output = result.stdout + result.stderr

    if "No scanners" in output or not output.strip():
        print("ERROR: No scanners found. Make sure your Canon TR7100 is")
        print("connected and powered on, then try again.")
        print("\nTroubleshooting:")
        print("  - Run 'scanimage -L' to list devices")
        print("  - Check USB/network connection")
        print("  - Install the Canon driver or sane-airscan package")
        sys.exit(1)

    # Parse device lines like: device `pixma:04A9190D_...' is a CANON ...
    devices = []
    for line in output.splitlines():
        if line.strip().startswith("device"):
            # Extract device string between backticks
            start = line.find("`")
            end = line.find("'", start + 1)
            if start != -1 and end != -1:
                dev = line[start + 1 : end]
                devices.append((dev, line.strip()))

    if not devices:
        print("ERROR: Could not parse scanner output:")
        print(output)
        sys.exit(1)

    if len(devices) == 1:
        device = devices[0][0]
        print(f"Found scanner: {devices[0][1]}")
        return device

    # Multiple scanners -- let the user choose
    print("Multiple scanners found:")
    for i, (dev, desc) in enumerate(devices, 1):
        print(f"  {i}. {desc}")

    while True:
        choice = input(f"Select scanner [1-{len(devices)}]: ").strip()
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(devices):
                return devices[idx][0]
        except ValueError:
            pass
        print("Invalid choice, try again.")


def get_adf_source(device):
    """Try to determine the correct ADF source name for this scanner."""
    result = subprocess.run(
        ["scanimage", f"--device={device}", "--help"],
        capture_output=True,
        text=True,
        timeout=15,
    )
    help_text = result.stdout + result.stderr

    # Common ADF source names across SANE backends
    adf_names = [
        "Automatic Document Feeder",
        "ADF",
        "ADF Front",
        "adf",
        "Auto",
    ]
    for name in adf_names:
        if name in help_text:
            return name

    # If we can't find a known ADF source, return None (will use default)
    return None


def scan_batch(device, output_dir, prefix, dpi, mode, source, paper_size):
    """Scan a batch of pages from the ADF, saving as individual images."""
    cmd = [
        "scanimage",
        f"--device={device}",
        f"--resolution={dpi}",
        f"--mode={mode}",
        "--format=png",
        "--batch=" + os.path.join(output_dir, f"{prefix}_%03d.png"),
        "--batch-count=0",  # scan until ADF is empty
    ]

    if source:
        cmd.append(f"--source={source}")

    # Paper size presets (width x height in mm)
    sizes = {
        "a4": (210, 297),
        "letter": (215.9, 279.4),
        "legal": (215.9, 355.6),
    }
    if paper_size.lower() in sizes:
        w, h = sizes[paper_size.lower()]
        cmd.extend([f"-x {w}", f"-y {h}"])

    print(f"\nScanning {prefix} pages...")
    print(f"  Command: {' '.join(cmd)}")
    print("  (Waiting for pages from document feeder...)\n")

    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except subprocess.TimeoutExpired:
        print("ERROR: Scanning timed out after 10 minutes.")
        sys.exit(1)

    # scanimage returns non-zero when ADF runs out -- that's expected
    if proc.returncode not in (0, 7):
        # Return code 7 is "document feeder out of documents" -- normal
        stderr = proc.stderr.strip()
        if "out of documents" in stderr.lower() or "jammed" not in stderr.lower():
            pass  # This is fine -- ADF ran empty
        else:
            print(f"WARNING: scanimage returned code {proc.returncode}")
            print(f"  stderr: {stderr}")

    # Collect scanned files
    pattern = os.path.join(output_dir, f"{prefix}_*.png")
    files = sorted(glob.glob(pattern))
    print(f"  Scanned {len(files)} page(s).")
    return files


def interleave_pages(fronts, backs):
    """
    Interleave front and back page files into the correct order.

    Fronts come in order: page 1, 3, 5, ...
    Backs come in REVERSE order from the ADF: last-back first.
    So backs need to be reversed to get: page 2, 4, 6, ...

    Result: page 1, page 2, page 3, page 4, ...
    """
    # Reverse backs because the ADF outputs them in reverse order
    # when you flip the stack upside-down
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

    # Convert all images to RGB mode for consistent PDF output
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
  5. Pages are interleaved and saved as a single PDF

Tips:
  - Make sure the ADF pickup roller is clean for reliable feeding
  - Do a test run with 2-3 pages first to verify page order
  - Use --fronts-only if you only need single-sided scanning via ADF
        """,
    )
    parser.add_argument(
        "-o",
        "--output",
        default=None,
        help="Output PDF filename (default: scan_YYYYMMDD_HHMMSS.pdf)",
    )
    parser.add_argument(
        "--dpi",
        type=int,
        default=300,
        help="Scan resolution in DPI (default: 300)",
    )
    parser.add_argument(
        "--mode",
        default="Color",
        choices=["Color", "Gray", "Lineart"],
        help="Scan mode (default: Color)",
    )
    parser.add_argument(
        "--device",
        default=None,
        help="SANE device name (auto-detected if omitted)",
    )
    parser.add_argument(
        "--paper",
        default="A4",
        choices=["A4", "Letter", "Legal"],
        help="Paper size (default: A4)",
    )
    parser.add_argument(
        "--fronts-only",
        action="store_true",
        help="Scan only front sides (skip duplex workflow)",
    )
    parser.add_argument(
        "--keep-images",
        action="store_true",
        help="Keep individual scanned images after creating PDF",
    )

    args = parser.parse_args()

    check_dependencies()

    device = detect_scanner(args.device)
    source = get_adf_source(device)

    if source:
        print(f"Using ADF source: {source}")
    else:
        print("Note: Could not detect ADF source name; using scanner default.")
        print("      If scanning fails, check 'scanimage --help -d <device>' for")
        print("      the correct --source value and pass --device manually.")

    # Create temp directory for scanned images
    work_dir = tempfile.mkdtemp(prefix="duplex_scan_")
    print(f"Working directory: {work_dir}")

    try:
        # --- Pass 1: Scan front sides ---
        print("\n" + "=" * 60)
        print("STEP 1: SCANNING FRONT SIDES")
        print("=" * 60)
        print("Load your documents FACE-UP in the ADF, page 1 on top.")
        input("Press ENTER when ready to scan front sides...")

        fronts = scan_batch(
            device, work_dir, "front", args.dpi, args.mode, source, args.paper
        )

        if not fronts:
            print("ERROR: No front pages were scanned. Check your scanner and ADF.")
            sys.exit(1)

        if args.fronts_only:
            # Single-sided mode
            output = args.output or f"scan_{datetime.now():%Y%m%d_%H%M%S}.pdf"
            create_pdf(fronts, output)
            print(f"\nDone! Single-sided scan saved to: {output}")
            return

        # --- Pass 2: Scan back sides ---
        print("\n" + "=" * 60)
        print("STEP 2: SCANNING BACK SIDES")
        print("=" * 60)
        print(f"\n{len(fronts)} front page(s) scanned successfully.")
        print()
        print("Now flip the pages for back-side scanning:")
        print("  1. Take the entire output stack from the scanner tray")
        print("  2. Flip the stack upside-down (do NOT reverse the order)")
        print("  3. Load the flipped stack back into the ADF")
        print()
        print("This means the LAST page's back will be scanned first,")
        print("and the tool will re-order them automatically.")
        input("\nPress ENTER when ready to scan back sides...")

        backs = scan_batch(
            device, work_dir, "back", args.dpi, args.mode, source, args.paper
        )

        if not backs:
            print("WARNING: No back pages scanned. Creating PDF with fronts only.")
            pages = fronts
        elif len(backs) != len(fronts):
            print(f"WARNING: Front pages ({len(fronts)}) != back pages ({len(backs)}).")
            print("         Pages will still be interleaved as best as possible.")
            pages = interleave_pages(fronts, backs)
        else:
            pages = interleave_pages(fronts, backs)

        # --- Create final PDF ---
        output = args.output or f"scan_{datetime.now():%Y%m%d_%H%M%S}.pdf"
        create_pdf(pages, output)
        print(f"\nDone! Duplex scan saved to: {output}")
        print(f"Total pages in PDF: {len(pages)}")

    finally:
        if not args.keep_images:
            shutil.rmtree(work_dir, ignore_errors=True)
        else:
            print(f"\nScanned images kept in: {work_dir}")


if __name__ == "__main__":
    main()
