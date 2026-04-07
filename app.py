import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import sys
import os
import re

# Import the main logic
try:
    from unitstacks_pipeline import UnitstacksPipeline
except ImportError:
    print("Error: Ensure this file is in the same directory as unitstacks_pipeline.py")
    sys.exit(1)

class RedirectText(object):
    def __init__(self, text_widget):
        self.text_widget = text_widget
        self.ansi_regex = re.compile(r'\x1b\[([0-9;]*)m')
        self.current_tags = []

    def write(self, string):
        self.text_widget.after(0, self._write, string)

    def _write(self, string):
        self.text_widget.configure(state="normal")
        parts = self.ansi_regex.split(string)
        for i, part in enumerate(parts):
            if i % 2 == 0:
                if part:
                    self.text_widget.insert(tk.END, part, tuple(self.current_tags))
            else:
                codes = part.split(';')
                for code in codes:
                    if code == '0' or code == '':
                        self.current_tags = []
                    elif code == '1':
                        if "bold" not in self.current_tags:
                            self.current_tags.append("bold")
                    else:
                        self.current_tags = [t for t in self.current_tags if not t.startswith("color_")]
                        self.current_tags.append(f"color_{code}")
        self.text_widget.see(tk.END)
        self.text_widget.configure(state="disabled")

    def flush(self):
        pass

class UnitstacksPipelineApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("HOI4 UnitStacks Generator")
        self.geometry("1000x700")
        self.minsize(900, 600)
        
        self.configure_styles()
        self.build_ui()
        
        sys.stdout = RedirectText(self.log_text)

    def configure_styles(self):
        self.style = ttk.Style(self)
        self.style.theme_use("clam")
        self.bg_color = "#f4f4f9"
        self.frame_bg = "#ffffff"
        self.accent_color = "#007acc"
        self.configure(bg=self.bg_color)
        
        self.style.configure(".", background=self.bg_color, font=("Segoe UI", 10))
        self.style.configure("Card.TFrame", background=self.frame_bg, relief="flat")
        self.style.configure("Header.TLabel", font=("Segoe UI", 16, "bold"), background=self.bg_color)
        self.style.configure("Card.TLabel", background=self.frame_bg, font=("Segoe UI", 10, "bold"))
        self.style.configure("Primary.TButton", font=("Segoe UI", 10, "bold"), background=self.accent_color, foreground="white")
        self.style.map("Primary.TButton", background=[("active", "#005f9e")])

    def build_ui(self):
        main_container = ttk.Frame(self, padding=20)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Header
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=tk.X, pady=(0, 10))
        ttk.Label(header_frame, text="HOI4 Unitstacks Pipeline", style="Header.TLabel").pack(anchor=tk.W)

        # --- MOD PATH SELECTION ---
        path_frame = ttk.Frame(main_container, style="Card.TFrame", padding=10)
        path_frame.pack(fill=tk.X, pady=(0, 20))
        
        ttk.Label(path_frame, text="Mod Directory:", style="Card.TLabel").pack(side=tk.LEFT, padx=(0, 10))
        self.path_var = tk.StringVar()
        self.path_entry = ttk.Entry(path_frame, textvariable=self.path_var)
        self.path_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 10))
        
        ttk.Button(path_frame, text="Browse", command=self.browse_folder).pack(side=tk.LEFT)

        # Content Area
        content_frame = ttk.Frame(main_container)
        content_frame.pack(fill=tk.BOTH, expand=True)

        # Left Panel
        controls_frame = ttk.Frame(content_frame, style="Card.TFrame", padding=20)
        controls_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))

        ttk.Label(controls_frame, text="Operation Mode:", style="Card.TLabel").pack(anchor=tk.W, pady=(0, 5))
        self.mode_var = tk.StringVar(value="pipeline")
        self.mode_dropdown = ttk.Combobox(controls_frame, textvariable=self.mode_var, 
                                         values=["pipeline", "validate", "repair", "generate"], 
                                         state="readonly", width=25)
        self.mode_dropdown.pack(anchor=tk.W, pady=(0, 15))

        ttk.Label(controls_frame, text="RNG Seed:", style="Card.TLabel").pack(anchor=tk.W, pady=(0, 5))
        self.seed_var = tk.StringVar()
        self.seed_entry = ttk.Entry(controls_frame, textvariable=self.seed_var, width=27)
        self.seed_entry.pack(anchor=tk.W, pady=(0, 25))

        self.run_button = ttk.Button(controls_frame, text="Run Pipeline", style="Primary.TButton", command=self.start_pipeline_thread)
        self.run_button.pack(fill=tk.X, pady=(0, 10))
        ttk.Button(controls_frame, text="Clear Logs", command=self.clear_logs).pack(fill=tk.X)

        # Right Panel (Logs)
        log_frame = ttk.Frame(content_frame, style="Card.TFrame", padding=1)
        log_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.log_text = tk.Text(log_frame, wrap=tk.WORD, state="disabled", bg="#1e1e1e", fg="#d4d4d4", 
                                font=("Consolas", 10), padx=10, pady=10, relief="flat", borderwidth=0)
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        
        # ANSI tags
        colors = {"31": "#ff5555", "91": "#ff5555", "32": "#50fa7b", "92": "#50fa7b", 
                  "33": "#f1fa8c", "93": "#f1fa8c", "36": "#8be9fd", "96": "#8be9fd"}
        self.log_text.tag_configure("bold", font=("Consolas", 10, "bold"))
        for code, hex in colors.items():
            self.log_text.tag_configure(f"color_{code}", foreground=hex)

    def browse_folder(self):
        folder = filedialog.askdirectory()
        if folder:
            self.path_var.set(os.path.normpath(folder))

    def clear_logs(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def start_pipeline_thread(self):
        mod_path = self.path_var.get().strip()
        if not mod_path or not os.path.isdir(mod_path):
            messagebox.showerror("Error", "Please select a valid Mod Directory first.")
            return

        self.run_button.configure(state="disabled")
        self.clear_logs()
        
        mode = self.mode_var.get()
        seed = int(self.seed_var.get()) if self.seed_var.get().isdigit() else None

        thread = threading.Thread(target=self.run_pipeline_logic, args=(mod_path, mode, seed), daemon=True)
        thread.start()

    def run_pipeline_logic(self, mod_path, mode, seed):
        # We override the internal paths by recreating the map folder structure 
        # relative to the user-provided mod_path.
        custom_paths = {
            'definitions': os.path.join(mod_path, "map/definition.csv"),
            'provinces': os.path.join(mod_path, "map/provinces.bmp"),
            'buildings': os.path.join(mod_path, "map/buildings.txt"),
            'unitstacks': os.path.join(mod_path, "map/unitstacks.txt")
        }

        # Initialize the pipeline and manually override its internal path dict
        pipeline = UnitstacksPipeline()
        pipeline.paths.update(custom_paths)
        
        try:
            if mode == "validate": pipeline.run_validation()
            elif mode == "repair": pipeline.run_repair()
            elif mode == "generate": pipeline.generate_unitstacks(seed=seed)
            elif mode == "pipeline": pipeline.run_pipeline(seed=seed)
        except Exception as e:
            print(f"\n\033[91m\033[1m[CRITICAL FAILURE]: {e}\033[0m")
        finally:
            print("\n>>> Process finished.")
            self.after(0, lambda: self.run_button.configure(state="normal"))

if __name__ == "__main__":
    app = UnitstacksPipelineApp()
    app.mainloop()
