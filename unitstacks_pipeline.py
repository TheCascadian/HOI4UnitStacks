# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True
"""Consolidated HOI4 unitstacks pipeline.

High-level purpose
------------------
This single-file tool combines validation, targeted repair, unitstack generation,
and a small orchestration runner. It is intentionally written to be converted
to a single Cython module (.pyx) if desired; types and hotspots are structured
so that conversion is straightforward.

Rationale / Why these choices were made
--------------------------------------
- Single-file: Combining validator, repairer and generator reduces import
    churn and simplifies packaging as a single compiled extension. Why: the
    pipeline is executed as an offline asset-generation step where startup
    overhead matters less than robustness and testability.
- 24-bit lookup array for province colors: provides O(1) color->province
    mapping at the cost of memory (~16.7M entries). Why: Python-level per-pixel
    dict lookups are the dominant CPU cost during center calculations; the
    memory tradeoff is acceptable for a desktop build tool.
- Conservative validation + single-pass repair: validate first, attempt a
    single automatic repair pass only when critical errors are found, then
    re-validate. Why: repeated auto-repair loops can oscillate or mask root
    causes; a single targeted fix keeps changes auditable and reversible.
- Snapping centers to nearest province pixel: centers computed from
    bincount averages may fall slightly off-map for thin or curved provinces.
    Snapping prevents generating invalid coordinates that cause engine "failed
    checks".

How to read this file (for maintainers)
---------------------------------------
- Top-level classes/functions:
    - `UnitstacksPipeline`: main class with validation, repair, and generation.
    - `main(...)`: thin CLI wrapper (modes: pipeline, validate, repair, generate).
- Look for `# THOUGHT:` blocks for design rationale where a maintainer
    would most likely want context before editing behavior.
 - Editing guidance:
    - To change rotation/offset behavior edit `TYPE_SETTINGS` and read the
        local `# THOUGHT:` block explaining constraints for ship and VP types.
    - To change fallback provinces, update `MISSING_PROVINCES`. Keep the
        set semantics (integers) and run `--mode validate` to check for missing
        definitions or zero-pixel provinces.

Cython conversion tips
----------------------
- Use memoryviews for the large NumPy arrays and annotate hot functions.
- Keep `boundscheck=False` on loops that are already guarded; use typed
    variables for tight loops (map processing) to get largest speedups.

Operational notes
-----------------
- Run `python unitstacks_pipeline.py --mode pipeline` to run the full flow.
- If you plan to compile to .pyx, keep `numpy` and `Pillow` in the environment.
"""

import argparse
import math
import os
import random
import shutil
import sys
from collections import defaultdict
from datetime import datetime

import numpy as np
from PIL import Image


class Colors:
    RED = "\033[91m"
    GREEN = "\033[92m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BOLD = "\033[1m"
    END = "\033[0m"


MAX_COORD_X = 6000
MAX_COORD_Z = 2500
MAX_HEIGHT = 100.0
MAX_PROVINCE_ID = 65535
VALID_UNIT_TYPES = set(range(39))

LAND_TYPES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 21, 22, 23, 24, 25, 26, 27, 28, 29, 38]
SEA_TYPES = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]
PORT_TYPES = [11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 30, 31, 32, 33, 34, 35, 36, 37]

LAND_TYPE_SET = set(LAND_TYPES)
SEA_TYPE_SET = set(SEA_TYPES)
PORT_TYPE_SET = set(PORT_TYPES)

MISSING_PROVINCES = {5579, 5555, 5656, 1452, 1885, 2871, 4885, 8546, 2381, 2847}

TYPE_SETTINGS = {
    0: (0.00, 0.00, 0.10, 0.80),
    9: (0.00, 0.00, 0.20, 0.90),
    10: (0.00, 0.00, 0.10, 0.50),
    19: (-1.57, -1.57, 0.00, 0.00),
    20: (0.00, 0.00, 0.15, 0.50),
    21: (0.00, 0.00, 0.10, 0.80),
    38: (0.00, 0.00, 0.00, 0.00),
    **{index: (0.00, 0.00, 0.10, 0.40) for index in list(range(1, 9)) + list(range(22, 30))},
    **{index: (0.00, 0.00, 0.15, 0.50) for index in list(range(11, 19)) + list(range(30, 38))},
}
DEFAULT_SETTINGS = (0.00, 0.00, 0.10, 0.30)


def get_dynamic_values(unit_type):
    # THOUGHT: Balancing randomness vs engine stability.
    # WHY: We want visual variety (random offsets/rotations) so maps don't
    # look identical each run, but excessive randomness (especially for
    # ships and victory-point anchors) can lead to entity collisions or
    # 'failed checks' in the engine. The strategy below uses type-specific
    # ranges (TYPE_SETTINGS) so sensitive types get narrow ranges and
    # decorative types get wider ranges.
    settings = TYPE_SETTINGS.get(unit_type, DEFAULT_SETTINGS)
    return random.uniform(settings[0], settings[1]), random.uniform(settings[2], settings[3])


class UnitstacksPipeline:
    def __init__(self, script_dir=None, root_dir=None):
        if script_dir is None:
            script_dir = os.path.dirname(os.path.abspath(__file__))
        if root_dir is None:
            root_dir = os.path.abspath(os.path.join(script_dir, "..", ".."))

        self.script_dir = script_dir
        self.root_dir = root_dir
        self.paths = {
            "definition": os.path.join(root_dir, "map", "definition.csv"),
            "provinces": os.path.join(root_dir, "map", "provinces.bmp"),
            "heightmap": os.path.join(root_dir, "map", "heightmap.bmp"),
            "buildings": os.path.join(root_dir, "map", "buildings.txt"),
            "unitstacks": os.path.join(root_dir, "map", "unitstacks.txt"),
            "backups": os.path.join(root_dir, "map", "backups"),
        }
        self._backup_dir = None
        self.reset_validation_state()

    def reset_validation_state(self):
        self.errors = []
        self.warnings = []
        self.stats = defaultdict(int)
        self.def_data = {}
        self.unitstacks_data = []
        self.unitstack_types_by_pid = defaultdict(set)
        self.buildings_data = []

    def backup_dir(self):
        if self._backup_dir is None:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            self._backup_dir = os.path.join(self.paths["backups"], timestamp)
        return self._backup_dir

    def print_banner(self, title, subtitle=None):
        print("\n" + "=" * 70)
        print(title)
        if subtitle:
            print(subtitle)
        print("=" * 70)

    def print_stage(self, label):
        print(f"\n{Colors.CYAN}{Colors.BOLD}{label}{Colors.END}")
        print("-" * 70)

    def log_error(self, file_name, line_number, message, critical=True):
        level = "CRITICAL" if critical else "WARNING"
        color = Colors.RED if critical else Colors.YELLOW
        entry = f"{color}[{level}]{Colors.END} {Colors.BOLD}{file_name}:{line_number}{Colors.END} - {message}"
        if critical:
            self.errors.append(entry)
        else:
            self.warnings.append(entry)

    def log_info(self, message):
        print(f"{Colors.CYAN}[INFO]{Colors.END} {message}")

    def check_file_exists(self, key):
        path = self.paths[key]
        if not os.path.exists(path):
            self.log_error(key, "N/A", f"File not found: {path}", critical=True)
            return False
        self.stats[f"{key}_exists"] = True
        return True

    @staticmethod
    def build_province_lookup(province_data):
        # THOUGHT: Use a dense 24-bit lookup table to convert RGB -> province ID
        # WHY: It gives fastest per-pixel mapping (O(1) array access). Alternatives
        # like dict mapping were measured to be much slower for large images.
        # Memory tradeoff: 16,777,216 ints; acceptable for desktop tooling.
        lookup = np.zeros(16777216, dtype=np.int32)
        for province_id, info in province_data.items():
            red, green, blue = info["rgb"]
            lookup[(red << 16) | (green << 8) | blue] = province_id
        return lookup

    def load_map_arrays(self):
        province_image = np.array(Image.open(self.paths["provinces"]).convert("RGB"), dtype=np.uint32)
        heightmap = np.array(Image.open(self.paths["heightmap"]).convert("L"), dtype=np.uint8)
        return province_image, heightmap

    def build_province_id_map(self, province_image, province_data):
        colors = (province_image[:, :, 0] << 16) | (province_image[:, :, 1] << 8) | province_image[:, :, 2]
        return self.build_province_lookup(province_data)[colors]

    @staticmethod
    def locate_land_province(pid_map, province_data, x_coord, z_coord):
        height, width = pid_map.shape
        pixel_x = int(round(x_coord))
        pixel_y = height - 1 - int(round(z_coord))
        offsets = [
            (0, 0),
            (1, 0),
            (-1, 0),
            (0, 1),
            (0, -1),
            (1, 1),
            (1, -1),
            (-1, 1),
            (-1, -1),
        ]

        for delta_x, delta_y in offsets:
            sample_x = pixel_x + delta_x
            sample_y = pixel_y + delta_y
            if not (0 <= sample_x < width and 0 <= sample_y < height):
                continue
            province_id = int(pid_map[sample_y, sample_x])
            if province_id in province_data and province_data[province_id]["type"] == "land":
                return province_id

        return None

    @staticmethod
    def snap_to_province_pixel(pid_map, target_pid, x_coord, y_image, max_radius=32):
        height, width = pid_map.shape
        pixel_x = int(round(x_coord))
        pixel_y = int(round(y_image))

        if 0 <= pixel_x < width and 0 <= pixel_y < height and int(pid_map[pixel_y, pixel_x]) == target_pid:
            return pixel_x, pixel_y

        best_match = None
        best_distance = None

        for radius in range(1, max_radius + 1):
            min_x = max(0, pixel_x - radius)
            max_x = min(width - 1, pixel_x + radius)
            min_y = max(0, pixel_y - radius)
            max_y = min(height - 1, pixel_y + radius)

            for sample_y in range(min_y, max_y + 1):
                for sample_x in range(min_x, max_x + 1):
                    if int(pid_map[sample_y, sample_x]) != target_pid:
                        continue

                    distance = (sample_x - x_coord) ** 2 + (sample_y - y_image) ** 2
                    if best_distance is None or distance < best_distance:
                        best_match = (sample_x, sample_y)
                        best_distance = distance

            if best_match is not None:
                return best_match

        return None

    @staticmethod
    def parse_naval_base_fields(parts):
        # THOUGHT: support multiple historical formats for buildings.txt
        # WHY: tools and exporters that generate `buildings.txt` have used
        # different field orders historically. Being permissive here reduces
        # manual pre-processing and avoids data loss when running the pipeline.
        if len(parts) < 7 or not parts[1].strip().lower().startswith("naval_base"):
            return None

        try:
            try:
                float(parts[2])
                x_coord = float(parts[2])
                y_coord = float(parts[3])
                z_coord = float(parts[4])
                rotation = float(parts[5])
                file_province = int(parts[6])
            except ValueError:
                file_province = int(parts[2])
                x_coord = float(parts[3])
                y_coord = float(parts[4])
                z_coord = float(parts[5])
                rotation = float(parts[6])
        except (ValueError, IndexError):
            return None

        return x_coord, y_coord, z_coord, rotation, file_province

    def _parse_definition_rows(self, validate=False):
        province_data = {}
        seen_ids = set()
        seen_colors = set()

        path = self.paths["definition"]
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue

                parts = line.split(";")
                minimum_fields = 8 if validate else 6
                if len(parts) < minimum_fields:
                    if validate:
                        self.log_error(
                            "definition.csv",
                            line_number,
                            f"Expected {minimum_fields}+ fields, found {len(parts)}: {line[:50]}...",
                            critical=True,
                        )
                    continue

                try:
                    province_id = int(parts[0])
                    red = int(parts[1])
                    green = int(parts[2])
                    blue = int(parts[3])
                except ValueError as exc:
                    if validate:
                        self.log_error("definition.csv", line_number, f"Parse error: {exc}", critical=True)
                    continue

                province_type = parts[4].strip().lower()
                is_coastal = len(parts) > 5 and parts[5].strip().lower() == "true"
                terrain = parts[6].strip() if len(parts) > 6 else ""
                continent = 0
                if len(parts) > 7:
                    try:
                        continent = int(parts[7])
                    except ValueError:
                        if validate:
                            self.log_error(
                                "definition.csv",
                                line_number,
                                f"Invalid continent value '{parts[7]}'",
                                critical=True,
                            )
                        continue

                if province_id == 0:
                    if validate:
                        self.log_error(
                            "definition.csv",
                            line_number,
                            "Province ID 0 (void province) found - treated as placeholder.",
                            critical=False,
                        )
                    continue

                if validate:
                    if province_id < 0 or province_id > MAX_PROVINCE_ID:
                        self.log_error(
                            "definition.csv",
                            line_number,
                            f"Province ID {province_id} out of engine bounds (0-{MAX_PROVINCE_ID})",
                            critical=True,
                        )
                        continue

                    if province_id in seen_ids:
                        self.log_error("definition.csv", line_number, f"Duplicate Province ID {province_id}", critical=True)
                        continue
                    seen_ids.add(province_id)

                    for value, name in ((red, "R"), (green, "G"), (blue, "B")):
                        if not (0 <= value <= 255):
                            self.log_error(
                                "definition.csv",
                                line_number,
                                f"Invalid {name} value {value} (must be 0-255)",
                                critical=True,
                            )

                    color_tuple = (red, green, blue)
                    if color_tuple in seen_colors:
                        self.log_error(
                            "definition.csv",
                            line_number,
                            f"Duplicate RGB color {color_tuple} for Province {province_id}",
                            critical=True,
                        )
                        continue
                    seen_colors.add(color_tuple)

                    if province_type not in ("land", "sea", "lake"):
                        self.log_error(
                            "definition.csv",
                            line_number,
                            f"Unknown province type '{province_type}' (expected: land, sea, lake)",
                            critical=True,
                        )

                province_data[province_id] = {
                    "rgb": (red, green, blue),
                    "type": province_type,
                    "is_land": province_type == "land",
                    "is_sea": province_type == "sea",
                    "is_coastal": is_coastal,
                    "terrain": terrain,
                    "continent": continent,
                    "has_port": False,
                    "port_info": None,
                    "line": line_number,
                }

        return province_data

    def load_generation_definitions(self):
        return self._parse_definition_rows(validate=False)

    def validate_definition_csv(self):
        if not self.check_file_exists("definition"):
            return

        self.log_info("Scanning definition.csv...")
        try:
            self.def_data = self._parse_definition_rows(validate=True)
        except Exception as exc:
            self.log_error("definition.csv", "N/A", f"File read error: {exc}", critical=True)
            self.def_data = {}

        self.stats["definition_provinces"] = len(self.def_data)
        self.log_info(f"Loaded {len(self.def_data)} province definitions")

    def validate_provinces_bmp(self):
        if not self.check_file_exists("provinces"):
            return

        self.log_info("Analyzing provinces.bmp...")

        try:
            province_image = np.array(Image.open(self.paths["provinces"]).convert("RGB"), dtype=np.uint32)
            height, width = province_image.shape[0], province_image.shape[1]
            self.stats["map_dimensions"] = f"{width}x{height}"
            self.log_info(f"Map dimensions: {width}x{height}")

            colors = (province_image[:, :, 0] << 16) | (province_image[:, :, 1] << 8) | province_image[:, :, 2]
            unique_colors = np.unique(colors)
            def_colors = {info["rgb"]: province_id for province_id, info in self.def_data.items()}

            orphan_colors = []
            for color_int in unique_colors:
                red = (color_int >> 16) & 0xFF
                green = (color_int >> 8) & 0xFF
                blue = color_int & 0xFF
                if (red, green, blue) not in def_colors:
                    orphan_colors.append((red, green, blue))

            if orphan_colors:
                self.log_error(
                    "provinces.bmp",
                    "N/A",
                    f"Found {len(orphan_colors)} colors in bitmap with no definition.csv entry!",
                    critical=False,
                )
                for index, color in enumerate(orphan_colors[:5], 1):
                    print(f"       Orphan color {index}: RGB{color}")
                if len(orphan_colors) > 5:
                    print(f"       ... and {len(orphan_colors) - 5} more")

            missing_from_bitmap = []
            for province_id, info in self.def_data.items():
                color_int = (info["rgb"][0] << 16) | (info["rgb"][1] << 8) | info["rgb"][2]
                if color_int not in unique_colors:
                    missing_from_bitmap.append(province_id)

            if missing_from_bitmap:
                self.log_error(
                    "definition.csv/provinces.bmp",
                    "N/A",
                    f"{len(missing_from_bitmap)} provinces defined but have 0 pixels in bitmap!",
                    critical=False,
                )
                print(f"       Example missing IDs: {missing_from_bitmap[:10]}")

            self.stats["bitmap_unique_colors"] = len(unique_colors)
        except Exception as exc:
            self.log_error("provinces.bmp", "N/A", f"Image processing error: {exc}", critical=True)

    def validate_buildings_txt(self):
        if not self.check_file_exists("buildings"):
            return

        self.log_info("Scanning buildings.txt...")
        seen_ports = set()

        try:
            province_image = np.array(Image.open(self.paths["provinces"]).convert("RGB"), dtype=np.uint32)
            pid_map = self.build_province_id_map(province_image, self.def_data) if self.def_data else None
        except Exception as exc:
            self.log_error("buildings.txt", "N/A", f"Failed loading provinces.bmp for cross-reference: {exc}", critical=True)
            pid_map = None

        try:
            with open(self.paths["buildings"], "r", encoding="utf-8-sig", errors="ignore") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    line = raw_line.strip()
                    if not line or line.startswith("#"):
                        continue

                    parts = line.split(";")
                    if len(parts) < 2 and "\t" in line:
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            "Line uses TABS instead of semicolons - engine will ignore!",
                            critical=True,
                        )
                        continue

                    if len(parts) < 7:
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"Too few fields ({len(parts)}): {line[:60]}...",
                            critical=True,
                        )
                        continue

                    try:
                        state_id = int(parts[0])
                    except ValueError as exc:
                        self.log_error("buildings.txt", line_number, f"Parse error: {exc}", critical=True)
                        continue

                    naval_base = self.parse_naval_base_fields(parts)
                    if naval_base is None:
                        continue

                    x_coord, y_coord, z_coord, rotation, file_province = naval_base
                    resolved_province = None
                    if pid_map is not None:
                        resolved_province = self.locate_land_province(pid_map, self.def_data, x_coord, z_coord)

                    if any(math.isnan(value) for value in (x_coord, y_coord, z_coord, rotation)):
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"NaN detected in coordinates for Province {file_province}!",
                            critical=True,
                        )
                        continue

                    if any(math.isinf(value) for value in (x_coord, y_coord, z_coord, rotation)):
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"Infinity detected in coordinates for Province {file_province}!",
                            critical=True,
                        )
                        continue

                    if x_coord < 0 or z_coord < 0:
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"Negative coordinates ({x_coord}, {z_coord}) for Province {file_province}! This places building off-map!",
                            critical=False,
                        )

                    if resolved_province is None:
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"Could not resolve a land province at naval base coordinates ({x_coord}, {z_coord}); file field is {file_province}.",
                            critical=False,
                        )
                        continue

                    if resolved_province not in self.def_data:
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"Naval base resolved to unknown Province ID {resolved_province}!",
                            critical=True,
                        )
                        continue

                    if resolved_province in seen_ports:
                        self.log_error(
                            "buildings.txt",
                            line_number,
                            f"Duplicate naval base for Province {resolved_province}! Engine will use first occurrence only.",
                            critical=False,
                        )
                    else:
                        seen_ports.add(resolved_province)

                    self.buildings_data.append(
                        {
                            "state": state_id,
                            "province": resolved_province,
                            "file_province": file_province,
                            "x": x_coord,
                            "y": y_coord,
                            "z": z_coord,
                            "rot": rotation,
                            "line": line_number,
                        }
                    )
        except Exception as exc:
            self.log_error("buildings.txt", "N/A", f"File read error: {exc}", critical=True)

        self.stats["naval_bases"] = len(self.buildings_data)
        self.log_info(f"Found {len(self.buildings_data)} valid naval bases")

    def validate_unitstacks_txt(self):
        if not self.check_file_exists("unitstacks"):
            return

        self.log_info("CRITICAL: Deep scanning unitstacks.txt for entity failures...")
        seen_entries = set()
        coord_bounds = {
            "min_x": None,
            "max_x": None,
            "min_z": None,
            "max_z": None,
        }

        nan_count = 0
        inf_count = 0
        void_province_count = 0
        invalid_type_count = 0
        out_of_bounds_count = 0

        try:
            with open(self.paths["unitstacks"], "r", encoding="utf-8-sig", errors="ignore") as handle:
                for line_number, raw_line in enumerate(handle, 1):
                    line = raw_line.strip()
                    if not line:
                        continue

                    if line.startswith("#") or line.startswith("//"):
                        self.log_error(
                            "unitstacks.txt",
                            line_number,
                            "Comment lines found - engine may not parse these correctly!",
                            critical=False,
                        )
                        continue

                    parts = line.split(";")
                    if len(parts) != 7:
                        self.log_error(
                            "unitstacks.txt",
                            line_number,
                            f"Expected exactly 7 fields (found {len(parts)}). Format must be: ProvID;Type;X;Y;Z;Rot;Offset",
                            critical=True,
                        )
                        continue

                    try:
                        province_id = int(parts[0])
                        unit_type = int(parts[1])
                        x_coord = float(parts[2])
                        y_coord = float(parts[3])
                        z_coord = float(parts[4])
                        rotation = float(parts[5])
                        offset = float(parts[6])
                    except ValueError as exc:
                        self.log_error(
                            "unitstacks.txt",
                            line_number,
                            f"Type conversion error: {exc}. Check for text in numeric fields.",
                            critical=True,
                        )
                        continue

                    if province_id == 0:
                        void_province_count += 1
                        if void_province_count <= 10:
                            self.log_error(
                                "unitstacks.txt",
                                line_number,
                                "Province ID 0 (void) found - treated as placeholder.",
                                critical=False,
                            )
                        continue

                    for value, label in ((x_coord, "X"), (y_coord, "Y"), (z_coord, "Z"), (rotation, "Rot"), (offset, "Offset")):
                        if math.isnan(value):
                            nan_count += 1
                            if nan_count <= 3:
                                self.log_error(
                                    "unitstacks.txt",
                                    line_number,
                                    f"CRITICAL: NaN in {label} coordinate! Engine cannot place entity - causes 'failed checks'!",
                                    critical=True,
                                )
                        if math.isinf(value):
                            inf_count += 1
                            if inf_count <= 3:
                                self.log_error(
                                    "unitstacks.txt",
                                    line_number,
                                    f"CRITICAL: Infinity in {label} coordinate! Engine cannot place entity!",
                                    critical=True,
                                )

                    if unit_type not in VALID_UNIT_TYPES:
                        invalid_type_count += 1
                        if invalid_type_count <= 3:
                            self.log_error(
                                "unitstacks.txt",
                                line_number,
                                f"Invalid unit type {unit_type} (valid: 0-38). Unknown types spawn 'failed checks'!",
                                critical=True,
                            )

                    if x_coord < 0 or x_coord > MAX_COORD_X or z_coord < 0 or z_coord > MAX_COORD_Z:
                        out_of_bounds_count += 1
                        if out_of_bounds_count <= 3:
                            self.log_error(
                                "unitstacks.txt",
                                line_number,
                                f"Coordinates ({x_coord}, {z_coord}) out of map bounds! Entity spawns in void - 'failed checks'!",
                                critical=True,
                            )

                    if y_coord < -10 or y_coord > MAX_HEIGHT:
                        self.log_error(
                            "unitstacks.txt",
                            line_number,
                            f"Suspicious Y-height {y_coord} (expected 0-{MAX_HEIGHT})",
                            critical=False,
                        )

                    entry_key = (province_id, unit_type)
                    if entry_key in seen_entries:
                        self.log_error(
                            "unitstacks.txt",
                            line_number,
                            f"Duplicate entry for Province {province_id}, Type {unit_type}",
                            critical=False,
                        )
                    else:
                        seen_entries.add(entry_key)

                    for bound_key, candidate in (("min_x", x_coord), ("max_x", x_coord), ("min_z", z_coord), ("max_z", z_coord)):
                        if coord_bounds[bound_key] is None:
                            coord_bounds[bound_key] = candidate
                        elif bound_key.startswith("min"):
                            coord_bounds[bound_key] = min(coord_bounds[bound_key], candidate)
                        else:
                            coord_bounds[bound_key] = max(coord_bounds[bound_key], candidate)

                    if province_id not in self.def_data:
                        self.log_error(
                            "unitstacks.txt",
                            line_number,
                            f"References unknown Province ID {province_id} (not in definition.csv)!",
                            critical=True,
                        )

                    self.unitstack_types_by_pid[province_id].add(unit_type)
                    self.unitstacks_data.append(
                        {
                            "pid": province_id,
                            "type": unit_type,
                            "x": x_coord,
                            "y": y_coord,
                            "z": z_coord,
                            "line": line_number,
                        }
                    )
        except Exception as exc:
            self.log_error("unitstacks.txt", "N/A", f"File read error: {exc}", critical=True)

        print(f"\n{Colors.BOLD}Unitstacks Analysis Summary:{Colors.END}")
        print(f"  Total entries parsed: {len(self.unitstacks_data)}")
        if coord_bounds["min_x"] is None:
            print("  Coordinate bounds: no valid entries parsed")
        else:
            print(
                f"  Coordinate bounds: X({coord_bounds['min_x']:.1f} to {coord_bounds['max_x']:.1f}), "
                f"Z({coord_bounds['min_z']:.1f} to {coord_bounds['max_z']:.1f})"
            )
        print(f"  NaN errors: {nan_count}")
        print(f"  Infinity errors: {inf_count}")
        print(f"  Void Province (ID 0) errors: {void_province_count}")
        print(f"  Invalid Type errors: {invalid_type_count}")
        print(f"  Out of Bounds errors: {out_of_bounds_count}")

        self.stats["unitstacks_entries"] = len(self.unitstacks_data)
        self.stats["unitstacks_nan_errors"] = nan_count
        self.stats["unitstacks_inf_errors"] = inf_count
        self.stats["unitstacks_invalid_type_errors"] = invalid_type_count
        self.stats["unitstacks_out_of_bounds_errors"] = out_of_bounds_count

    def cross_reference_files(self):
        self.log_info("Cross-referencing all files...")

        if self.buildings_data and self.unitstacks_data:
            unitstack_provinces = {entry["pid"] for entry in self.unitstacks_data}
            missing_unitstacks = []
            for building in self.buildings_data:
                if building["province"] not in unitstack_provinces:
                    missing_unitstacks.append(building["province"])

            if missing_unitstacks:
                preview = missing_unitstacks[:10]
                suffix = "..." if len(missing_unitstacks) > 10 else ""
                self.log_error(
                    "CROSS-REF",
                    "N/A",
                    f"Provinces with naval bases but NO unitstacks entries: {preview}{suffix}",
                    critical=True,
                )

        if self.def_data and self.unitstacks_data:
            land_provinces = {province_id for province_id, info in self.def_data.items() if info["type"] == "land"}
            unitstack_type_zero = {entry["pid"] for entry in self.unitstacks_data if entry["type"] == 0}
            missing_type_zero = land_provinces - unitstack_type_zero
            if missing_type_zero:
                self.log_error(
                    "CROSS-REF",
                    "N/A",
                    f"{len(missing_type_zero)} land provinces missing Type 0 (standstill) unitstack! Units will be invisible in these provinces.",
                    critical=False,
                )
                print(f"       Example provinces: {list(missing_type_zero)[:5]}")

        if self.def_data and self.unitstack_types_by_pid:
            port_provinces = {building["province"] for building in self.buildings_data}
            invalid_layouts = []
            missing_layouts = []

            for province_id, info in self.def_data.items():
                present = self.unitstack_types_by_pid.get(province_id)
                if not present:
                    continue

                if info["type"] == "land":
                    expected = LAND_TYPE_SET | PORT_TYPE_SET if province_id in port_provinces else LAND_TYPE_SET
                else:
                    expected = SEA_TYPE_SET

                extras = sorted(present - expected)
                missing = sorted(expected - present)

                if extras:
                    invalid_layouts.append((province_id, info["type"], extras))
                if missing:
                    missing_layouts.append((province_id, info["type"], missing))

            if invalid_layouts:
                sample = ", ".join(
                    f"{province_id} ({province_type}): {values[:5]}{'...' if len(values) > 5 else ''}"
                    for province_id, province_type, values in invalid_layouts[:5]
                )
                self.log_error(
                    "CROSS-REF",
                    "N/A",
                    f"{len(invalid_layouts)} provinces have illegal unitstack types for their province class. Examples: {sample}",
                    critical=True,
                )

            if missing_layouts:
                sample = ", ".join(
                    f"{province_id} ({province_type}): {values[:5]}{'...' if len(values) > 5 else ''}"
                    for province_id, province_type, values in missing_layouts[:5]
                )
                self.log_error(
                    "CROSS-REF",
                    "N/A",
                    f"{len(missing_layouts)} provinces are missing required unitstack types for their province class. Examples: {sample}",
                    critical=True,
                )

    def generate_report(self):
        print("\n" + "=" * 70)
        print(f"{Colors.BOLD}HOI4 MAP VALIDATION REPORT{Colors.END}")
        print("=" * 70)

        print(f"\n{Colors.BOLD}Statistics:{Colors.END}")
        for key, value in self.stats.items():
            print(f"  {key}: {value}")

        if self.errors:
            print(f"\n{Colors.RED}{Colors.BOLD}CRITICAL ERRORS ({len(self.errors)}):{Colors.END}")
            for entry in self.errors[:20]:
                print(entry)
            if len(self.errors) > 20:
                print(f"\n  ... and {len(self.errors) - 20} more critical errors")

        if self.warnings:
            print(f"\n{Colors.YELLOW}{Colors.BOLD}WARNINGS ({len(self.warnings)}):{Colors.END}")
            for entry in self.warnings[:10]:
                print(entry)
            if len(self.warnings) > 10:
                print(f"\n  ... and {len(self.warnings) - 10} more warnings")

        print("\n" + "=" * 70)
        if not self.errors:
            print(f"{Colors.GREEN}{Colors.BOLD}VALIDATION PASSED - No critical errors found!{Colors.END}")
            if self.warnings:
                print(f"{Colors.YELLOW}But review warnings above.{Colors.END}")
            return True

        print(f"{Colors.RED}{Colors.BOLD}VALIDATION FAILED - Fix critical errors before launching game!{Colors.END}")
        return False

    def run_validation(self):
        self.reset_validation_state()
        self.print_banner("HOI4 MAP VALIDATOR", "Consolidated validation pass")
        self.validate_definition_csv()
        self.validate_provinces_bmp()
        self.validate_buildings_txt()
        self.validate_unitstacks_txt()
        self.cross_reference_files()
        return self.generate_report()

    def backup_file(self, file_path):
        if not os.path.exists(file_path):
            return False

        os.makedirs(self.backup_dir(), exist_ok=True)
        backup_path = os.path.join(self.backup_dir(), os.path.basename(file_path))
        shutil.copy2(file_path, backup_path)
        print(f"   [Backup] {os.path.basename(file_path)} -> {backup_path}")
        return True

    def repair_definition_csv(self):
        path = self.paths["definition"]
        if not os.path.exists(path):
            print("[!] definition.csv not found")
            return False

        print("-> Repairing definition.csv...")
        self.backup_file(path)

        fixed_lines = []
        removed_count = 0
        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue

                parts = line.split(";")
                if len(parts) >= 5:
                    pid_str = parts[0].strip()
                    if not pid_str.isdigit():
                        removed_count += 1
                        continue
                    fixed_lines.append(line)
                else:
                    removed_count += 1

        if removed_count == 0:
            print("   [Fixed] No corrupt entries found | Left file unchanged")
            return True

        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.write("\r\n".join(fixed_lines) + "\r\n")

        print(f"   [Fixed] Processed file | Removed {removed_count} corrupt entries")
        return True

    def repair_buildings_txt(self):
        path = self.paths["buildings"]
        if not os.path.exists(path):
            print("[!] buildings.txt not found")
            return False

        print("-> Repairing buildings.txt (removing duplicate ports)...")
        self.backup_file(path)

        seen_lines = set()
        fixed_lines = []
        removed_count = 0

        with open(path, "r", encoding="utf-8-sig", errors="ignore") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.strip()

                if not line or line.startswith("#"):
                    fixed_lines.append(raw_line)
                    continue

                parts = line.split(";")
                if len(parts) >= 7 and parts[1].strip().lower().startswith("naval_base"):
                    if line in seen_lines:
                        removed_count += 1
                        if removed_count <= 5:
                            print(f"   Removed exact duplicate naval base at line {line_number}")
                        continue
                    seen_lines.add(line)

                fixed_lines.append(raw_line)

        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(fixed_lines)

        print(f"   [Fixed] Removed {removed_count} exact duplicate naval base lines")
        return True

    def repair_unitstacks_txt(self):
        path = self.paths["unitstacks"]
        if not os.path.exists(path):
            return False

        print("-> Repairing unitstacks.txt (cleaning invalid entries)...")
        self.backup_file(path)

        fixed_lines = []
        removed_count = 0
        with open(path, "r", encoding="utf-8", errors="ignore") as handle:
            for line_number, raw_line in enumerate(handle, 1):
                line = raw_line.strip()
                if not line:
                    fixed_lines.append(raw_line)
                    continue

                parts = line.split(";")
                if len(parts) != 7:
                    removed_count += 1
                    continue

                try:
                    province_id = int(parts[0])
                    values = [float(parts[index]) for index in range(2, 7)]
                except (ValueError, IndexError):
                    removed_count += 1
                    continue

                if province_id == 0:
                    removed_count += 1
                    print(f"   Removed Province 0 entry at line {line_number}")
                    continue

                if any(math.isnan(value) or math.isinf(value) for value in values):
                    removed_count += 1
                    print(f"   Removed NaN/Inf entry at line {line_number}")
                    continue

                fixed_lines.append(raw_line)

        with open(path, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(fixed_lines)

        print(f"   [Fixed] Removed {removed_count} invalid entries")
        return True

    def run_repair(self):
        self.print_banner("HOI4 MAP REPAIR UTILITY", f"Backups will be saved to: {self.backup_dir()}")

        processed = []
        if self.repair_definition_csv():
            processed.append("definition.csv")
        print()

        if self.repair_buildings_txt():
            processed.append("buildings.txt")
        print()

        if self.repair_unitstacks_txt():
            processed.append("unitstacks.txt")
        print()

        print("=" * 70)
        if processed:
            print(f"PROCESSED: {', '.join(processed)}")
            print(f"Backup location: {self.backup_dir()}")
            return True

        print("Nothing to repair or files not found")
        return False

    def extract_port_data(self, province_data, province_id_map):
        if not os.path.exists(self.paths["buildings"]):
            return 0
        # THOUGHT: We prefer locating ports by spatial coordinates and snapping to
        # nearest land pixel rather than trusting the numeric province ID in the
        # file. WHY: file-based province IDs can be inconsistent or refer to a
        # different enumeration; spatial resolution is more robust.

        found = 0
        with open(self.paths["buildings"], "r", encoding="utf-8", errors="ignore") as handle:
            for raw_line in handle:
                parts = raw_line.strip().split(";")
                naval_base = self.parse_naval_base_fields(parts)
                if naval_base is None:
                    continue

                x_coord, y_coord, z_coord, rotation, _ = naval_base
                province_id = self.locate_land_province(province_id_map, province_data, x_coord, z_coord)
                if province_id is None:
                    continue

                snapped = self.snap_to_province_pixel(
                    province_id_map,
                    province_id,
                    x_coord,
                    province_id_map.shape[0] - 1 - z_coord,
                )
                if snapped is not None:
                    x_coord = float(snapped[0])
                    z_coord = float(province_id_map.shape[0] - 1 - snapped[1])

                if province_data[province_id]["has_port"]:
                    continue

                province_data[province_id]["has_port"] = True
                province_data[province_id]["port_info"] = (x_coord, y_coord, z_coord, rotation)
                found += 1

        return found

    def calculate_centers(self, province_image, heightmap, province_data, province_id_map):
        height, width = province_image.shape[0], province_image.shape[1]
        y_indices, x_indices = np.indices((height, width))
        flat_ids = province_id_map.ravel()

        counts = np.bincount(flat_ids)
        sum_x = np.bincount(flat_ids, weights=x_indices.ravel())
        sum_y = np.bincount(flat_ids, weights=y_indices.ravel())

        centers = {}
        # THOUGHT: We compute centers using bincount averages then snap to a
        # valid pixel. WHY: pure centroid can fall on a neighboring thin pixel
        # (especially on coastlines). Snapping ensures generated coordinates
        # are always inside a province and sample the heightmap reliably.
        for province_id in np.where(counts > 0)[0]:
            if province_id == 0 or province_id not in province_data:
                continue

            center_x = sum_x[province_id] / counts[province_id]
            center_y_image = sum_y[province_id] / counts[province_id]

            snapped = self.snap_to_province_pixel(province_id_map, province_id, center_x, center_y_image)
            if snapped is not None:
                center_x = float(snapped[0])
                center_y_image = float(snapped[1])

            sample_x = int(center_x)
            sample_y = int(center_y_image)
            if 0 <= sample_x < width and 0 <= sample_y < height:
                game_y = (float(heightmap[sample_y, sample_x]) / 255.0) * 25.5
            else:
                game_y = 0.0

            center_z = height - 1 - center_y_image
            centers[province_id] = (center_x, game_y, center_z)

        return centers

    def generate_unitstacks(self, seed=None):
        if seed is not None:
            random.seed(seed)

        province_data = self.load_generation_definitions()
        if not province_data:
            print("[ERROR] No province definitions loaded.")
            return False

        province_image, heightmap = self.load_map_arrays()
        province_id_map = self.build_province_id_map(province_image, province_data)
        ports_found = self.extract_port_data(province_data, province_id_map)
        centers = self.calculate_centers(province_image, heightmap, province_data, province_id_map)

        print(
            f"{Colors.CYAN}[INGEST]{Colors.END} "
            f"Loaded {len(province_data)} provs | Extracted {ports_found} ports | Resolved {len(centers)} centers"
        )

        lines = []
        generated_provinces = set()
        injected = 0

        for province_id, info in province_data.items():
            if province_id not in centers:
                continue

            center_x, center_y, center_z = centers[province_id]
            generated_provinces.add(province_id)
            # THOUGHT: choose base_types from province classification
            # WHY: Land and sea provinces have distinct valid unit layouts;
            # keeping these lists centralised (LAND_TYPES/SEA_TYPES) simplifies
            # audits and cross-reference checks.
            base_types = LAND_TYPES if info["is_land"] else SEA_TYPES
            for unit_type in base_types:
                rotation, offset = get_dynamic_values(unit_type)
                lines.append(
                    f"{province_id};{unit_type};{center_x:.2f};{center_y:.2f};{center_z:.2f};{rotation:.2f};{offset:.2f}"
                )

            if info["is_land"] and info["has_port"] and info["port_info"]:
                port_x, port_y, port_z, port_rot = info["port_info"]
                for unit_type in PORT_TYPES:
                    _, offset = get_dynamic_values(unit_type)
                    lines.append(
                        f"{province_id};{unit_type};{port_x:.2f};{port_y:.2f};{port_z:.2f};{port_rot:.2f};{offset:.2f}"
                    )

        # THOUGHT: emergency injector - fill known problematic provinces
        # WHY: historically certain provinces are defined but produce zero
        # pixels or are dropped earlier in processing; we inject sensible
        # defaults to avoid fatal engine errors while keeping a log for manual fix.
        for province_id in MISSING_PROVINCES:
            if province_id in generated_provinces or province_id not in province_data:
                continue

            if not province_data[province_id]["port_info"]:
                continue

            port_x, port_y, port_z, port_rot = province_data[province_id]["port_info"]
            injected_types = LAND_TYPES + PORT_TYPES if province_data[province_id]["is_land"] else SEA_TYPES
            for unit_type in injected_types:
                rotation = port_rot if unit_type in PORT_TYPES else get_dynamic_values(unit_type)[0]
                offset = 0.10 if unit_type in PORT_TYPES else get_dynamic_values(unit_type)[1]
                lines.append(
                    f"{province_id};{unit_type};{port_x:.2f};{port_y:.2f};{port_z:.2f};{rotation:.2f};{offset:.2f}"
                )
            injected += 1

        lines.sort(key=lambda line: (int(line.split(";")[1]), int(line.split(";")[0])))

        with open(self.paths["unitstacks"], "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")

        print(
            f"{Colors.GREEN}[SUCCESS]{Colors.END} "
            f"Generated {len(lines)} lines | Forced {injected} fallback ports | File written"
        )
        return True

    def run_pipeline(self, seed=None):
        self.print_banner("HOI4 UNITSTACKS PIPELINE", "Validate -> Repair -> Validate -> Generate -> Validate")

        self.print_stage("STAGE 1: INITIAL VALIDATION")
        initial_valid = self.run_validation()

        repair_ran = False
        if not initial_valid:
            self.print_stage("STAGE 2: REPAIR")
            repair_ran = self.run_repair()

            self.print_stage("STAGE 3: POST-REPAIR VALIDATION")
            repaired_valid = self.run_validation()
            if not repaired_valid:
                print(
                    "\nPost-repair validation still reports critical errors. "
                    "Continuing to generation because regenerating unitstacks can resolve stale-output failures."
                )
        else:
            self.print_stage("STAGE 2: REPAIR")
            print("No critical errors detected. Skipping repair.")

        self.print_stage("STAGE 4: GENERATION")
        if not self.generate_unitstacks(seed=seed):
            print("[FATAL] Unitstack generation failed.")
            return False

        self.print_stage("STAGE 5: FINAL VALIDATION")
        final_valid = self.run_validation()
        if not final_valid:
            print("\nFATAL: Generated output still fails validation.")
            return False

        self.print_banner("PIPELINE COMPLETE")
        if repair_ran:
            print(f"Backups: {self.backup_dir()}")
        print(f"Output: {self.paths['unitstacks']}")
        return True


def build_argument_parser():
    parser = argparse.ArgumentParser(description="Consolidated HOI4 unitstacks pipeline.")
    parser.add_argument(
        "--mode",
        choices=("pipeline", "validate", "repair", "generate"),
        default="pipeline",
        help="Which stage to run.",
    )
    parser.add_argument("--seed", type=int, default=None, help="Optional RNG seed for repeatable offsets.")
    return parser


def main(argv=None):
    args = build_argument_parser().parse_args(argv)
    pipeline = UnitstacksPipeline()

    if args.mode == "validate":
        return 0 if pipeline.run_validation() else 1
    if args.mode == "repair":
        return 0 if pipeline.run_repair() else 1
    if args.mode == "generate":
        return 0 if pipeline.generate_unitstacks(seed=args.seed) else 1
    return 0 if pipeline.run_pipeline(seed=args.seed) else 1


if __name__ == "__main__":
    sys.exit(main())