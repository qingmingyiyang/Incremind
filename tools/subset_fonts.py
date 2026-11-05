import argparse
import json
from pathlib import Path

import brotli
from fontTools import __version__ as fonttools_version
from fontTools import subset
from fontTools.ttLib import TTFont
from fontTools.varLib.instancer import instantiateVariableFont


ROOT = Path(__file__).resolve().parents[1]
FONT_ROOT = ROOT / "src/frontend/public/fonts"
FACE_PATH = ROOT / "src/frontend/src/font-faces.css"
CHINESE_FONTS = (
    ("Noto Sans SC", "400", "noto-sans-sc-400.ttf", "noto-sans-cjk-sc-400.otf"),
    ("Noto Sans SC", "500", "noto-sans-sc-500.ttf", "noto-sans-cjk-sc-500.otf"),
    ("Noto Serif SC", "700", "noto-serif-sc-700.ttf", "noto-serif-cjk-sc-700.otf"),
)
RARE_SLICE_SIZE = 512
SUPPLEMENTAL = {0x20BB7}


def unicode_ranges(codepoints):
    ordered = sorted(codepoints)
    ranges = []
    for value in ordered:
        if ranges and value == ranges[-1][1] + 1:
            ranges[-1][1] = value
        else:
            ranges.append([value, value])
    return ",".join(f"U+{start:X}" if start == end else f"U+{start:X}-{end:X}" for start, end in ranges)


def parse_ranges(text):
    result = set()
    for part in text.split(","):
        values = part.removeprefix("U+").split("-")
        result.update(range(int(values[0], 16), int(values[-1], 16) + 1))
    return result


def common_codepoints():
    # GB2312 一级汉字与常用标点使用稳定字符集，输入资料中的其余字符按需读取。
    characters = set(range(0x20, 0x100)) | set(range(0x2000, 0x2070)) | set(range(0x3000, 0x3040))
    for lead in range(0xB0, 0xD8):
        final = 0xFA if lead == 0xD7 else 0xFF
        for trail in range(0xA1, final):
            characters.add(ord(bytes((lead, trail)).decode("gb2312")))
    return characters


def write_subset(source, destination, codepoints, variable=False):
    with TTFont(source, recalcTimestamp=False) as font:
        if not codepoints <= font.getBestCmap().keys():
            raise ValueError(f"{source}: missing requested Unicode characters")
        if variable:
            instantiateVariableFont(font, {"wght": (400, 500)}, inplace=True)
        options = subset.Options()
        options.layout_features = ["*"]
        options.name_IDs = ["*"]
        options.name_languages = ["*"]
        options.glyph_names = True
        options.notdef_outline = True
        options.recalc_timestamp = False
        # 可变等宽字体保留完整字符集合，避免无意义地再次裁剪 gvar。
        if not variable:
            subsetter = subset.Subsetter(options=options)
            subsetter.populate(unicodes=codepoints)
            subsetter.subset(font)
        font.flavor = "woff2"
        font.save(destination)
    with TTFont(destination) as result:
        actual = set(result.getBestCmap())
        if actual != codepoints:
            raise ValueError(f"{destination}: output coverage differs from requested characters")


def font_face(family, weight, filename, ranges):
    return (
        "@font-face {\n"
        f'  font-family: "{family}";\n'
        "  font-style: normal;\n"
        f"  font-weight: {weight};\n"
        "  font-display: swap;\n"
        f'  src: url("/fonts/{filename}") format("woff2");\n'
        f"  unicode-range: {ranges};\n"
        "}\n"
    )


def generate(source_root, only_family=None):
    required = [name for _, _, primary, supplement in CHINESE_FONTS for name in (primary, supplement)]
    required.append("fira-code-variable.ttf")
    for name in required:
        if not (source_root / name).is_file():
            raise FileNotFoundError(f"Font source required: {source_root / name}")
    FONT_ROOT.mkdir(parents=True, exist_ok=True)
    manifest = {"fonttools": fonttools_version, "brotli": brotli.__version__, "rare_slice_size": RARE_SLICE_SIZE, "fonts": [], "files": []}
    faces = ["/* 由 tools/subset_fonts.py 生成；修改源脚本后重新生成。 */\n"]

    def emit(family, weight, filename, source, characters, variable=False):
        target = FONT_ROOT / filename
        if only_family is None or family == only_family:
            write_subset(source, target, characters, variable=variable)
        elif not target.is_file():
            raise FileNotFoundError(f"Unselected font output required: {target}")
        ranges = unicode_ranges(characters)
        manifest["files"].append({"file": filename, "family": family, "weight": weight, "bytes": target.stat().st_size, "unicode_range": ranges, "codepoints": len(characters)})
        faces.append(font_face(family, weight, filename, ranges))

    common = common_codepoints()
    for family, weight, name, supplemental_name in CHINESE_FONTS:
        source = source_root / name
        with TTFont(source) as font:
            coverage = set(font.getBestCmap())
        selected = coverage & common
        stem = name.removesuffix(".ttf")
        emit(family, weight, f"{stem}-common.woff2", source, selected)
        remaining = sorted(coverage - selected)
        for offset in range(0, len(remaining), RARE_SLICE_SIZE):
            emit(family, weight, f"{stem}-rare-{offset // RARE_SLICE_SIZE:03}.woff2", source, set(remaining[offset:offset + RARE_SLICE_SIZE]))
        emit(family, weight, f"{stem}-supplement.woff2", source_root / supplemental_name, SUPPLEMENTAL)
        manifest["fonts"].append({"family": family, "weight": weight, "source": name, "original_codepoints": len(coverage), "original_unicode_range": unicode_ranges(coverage), "preserved_codepoints": len(coverage), "supplemental_codepoints": sorted(SUPPLEMENTAL)})

    fira_source = source_root / "fira-code-variable.ttf"
    with TTFont(fira_source) as font:
        coverage = set(font.getBestCmap())
    emit("Fira Code", "400 500", "fira-code-400-500.woff2", fira_source, coverage, variable=True)
    manifest["fonts"].append({"family": "Fira Code", "weight": "400 500", "source": fira_source.name, "original_codepoints": len(coverage), "original_unicode_range": unicode_ranges(coverage), "preserved_codepoints": len(coverage), "supplemental_codepoints": []})
    FACE_PATH.write_text("\n".join(faces), encoding="utf-8", newline="\n")
    (FONT_ROOT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")


def verify():
    manifest = json.loads((FONT_ROOT / "manifest.json").read_text(encoding="utf-8"))
    expected_files = {asset["file"] for asset in manifest["files"]}
    actual_files = {path.name for path in FONT_ROOT.glob("*.woff2")}
    if expected_files != actual_files:
        raise ValueError("Font directory differs from the generated manifest")
    coverage = {}
    total = 0
    for asset in manifest["files"]:
        path = FONT_ROOT / asset["file"]
        with TTFont(path) as font:
            actual = set(font.getBestCmap())
            if actual != parse_ranges(asset["unicode_range"]) or len(actual) != asset["codepoints"]:
                raise ValueError(f"{path}: cmap differs from unicode-range")
            if asset["family"] == "Fira Code":
                axis = font["fvar"].axes[0]
                if (axis.axisTag, axis.minValue, axis.maxValue) != ("wght", 400, 500):
                    raise ValueError("Fira Code must retain both design weights")
        key = (asset["family"], asset["weight"])
        used = coverage.setdefault(key, set())
        if used & actual:
            raise ValueError(f"{path}: Unicode slice overlaps another slice")
        used.update(actual)
        if path.stat().st_size != asset["bytes"]:
            raise ValueError(f"{path}: byte count differs from manifest")
        total += asset["bytes"]
    for entry in manifest["fonts"]:
        original = parse_ranges(entry["original_unicode_range"])
        expected = original | set(entry["supplemental_codepoints"])
        actual = coverage[(entry["family"], entry["weight"])]
        if actual != expected or len(original) != entry["original_codepoints"]:
            raise ValueError(f"{entry['family']} {entry['weight']}: original coverage was not preserved")
        if entry["family"] != "Fira Code" and not {ord("龘"), ord("𠮷")} <= actual:
            raise ValueError(f"{entry['family']} {entry['weight']}: required rare characters missing")
    print(json.dumps({"files": len(expected_files), "font_bytes": total, "coverage": [{"family": family, "weight": weight, "codepoints": len(points)} for (family, weight), points in coverage.items()]}, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description="Generate and verify local WOFF2 Unicode slices")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--only-family", choices=("Noto Sans SC", "Noto Serif SC", "Fira Code"))
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if not args.check:
        if args.source_dir is None:
            parser.error("--source-dir is required when generating fonts")
        generate(args.source_dir.resolve(), only_family=args.only_family)
    verify()


if __name__ == "__main__":
    main()
