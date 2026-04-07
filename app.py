import tkinter as tk
from tkinter import ttk
import threading
import sys
import os

# Import the main logic from your provided script
try:
    from unitstacks_pipeline import UnitstacksPipeline
except ImportError:
    print("Error: Ensure this file is in the same directory as unitstacks_pipeline.py")
    sys.exit(1)


class RedirectText(object):
    """Redirects stdout to a tkinter Text widget safely across threads."""
    def __init__(self, text_widget):
        self.text_widget = text_widget

    def write(self, string):
        self.text_widget.after(0, self._write, string)

    def _write(self, string):
        self.text_widget.configure(state="normal")
        self.text_widget.insert(tk.END, string)
        self.text_widget.see(tk.END)
        self.text_widget.configure(state="disabled")

    def flush(self):
        pass


class UnitstacksPipelineApp(tk.Tk):
    def __init__(self):
        super().__init__()

        self.title("HOI4 UnitStacks Generator (GUI)")
        self.geometry("900x600")
        self.minsize(800, 500)
        
        self.configure_styles()
        self.build_ui()
        
        # Redirect standard output to the text widget
        sys.stdout = RedirectText(self.log_text)

    def configure_styles(self):
        """Sets up a modern, flat appearance using standard ttk."""
        self.style = ttk.Style(self)
        self.style.theme_use("clam")

        # Color palette
        self.bg_color = "#f4f4f9"
        self.frame_bg = "#ffffff"
        self.text_color = "#333333"
        self.accent_color = "#007acc"
        self.accent_hover = "#005f9e"

        self.configure(bg=self.bg_color)

        # Global configurations
        self.style.configure(".", background=self.bg_color, foreground=self.text_color, font=("Segoe UI", 10))
        
        # Frame styles
        self.style.configure("Card.TFrame", background=self.frame_bg, relief="flat")
        
        # Label styles
        self.style.configure("Header.TLabel", font=("Segoe UI", 16, "bold"), background=self.bg_color)
        self.style.configure("SubHeader.TLabel", font=("Segoe UI", 10), background=self.frame_bg, foreground="#666666")
        self.style.configure("Card.TLabel", background=self.frame_bg, font=("Segoe UI", 10, "bold"))
        
        # Button styles
        self.style.configure(
            "Primary.TButton", 
            font=("Segoe UI", 10, "bold"), 
            background=self.accent_color, 
            foreground="white", 
            borderwidth=0, 
            focuscolor=self.accent_color
        )
        self.style.map(
            "Primary.TButton", 
            background=[("active", self.accent_hover)]
        )

        # Entry and Combobox styles
        self.style.configure("TEntry", fieldbackground=self.frame_bg, borderwidth=1)
        self.style.configure("TCombobox", fieldbackground=self.frame_bg, borderwidth=1)

    def build_ui(self):
        """Constructs the layout of the application."""
        # Main container with padding
        main_container = ttk.Frame(self, padding=20)
        main_container.pack(fill=tk.BOTH, expand=True)

        # Header
        header_frame = ttk.Frame(main_container)
        header_frame.pack(fill=tk.X, pady=(0, 20))
        
        ttk.Label(header_frame, text="HOI4 Unitstacks Pipeline", style="Header.TLabel").pack(anchor=tk.W)
        ttk.Label(header_frame, text="Validate, repair, and generate unitstacks data.", style="SubHeader.TLabel", background=self.bg_color).pack(anchor=tk.W)

        # Content area split (Controls on left, Logs on right)
        content_frame = ttk.Frame(main_container)
        content_frame.pack(fill=tk.BOTH, expand=True)

        # --- LEFT PANEL: Controls ---
        controls_frame = ttk.Frame(content_frame, style="Card.TFrame", padding=20)
        controls_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))

        ttk.Label(controls_frame, text="Configuration", style="Card.TLabel").pack(anchor=tk.W, pady=(0, 15))

        # Mode Selection
        ttk.Label(controls_frame, text="Operation Mode:", style="Card.TLabel").pack(anchor=tk.W, pady=(0, 5))
        self.mode_var = tk.StringVar(value="pipeline")
        modes = ["pipeline", "validate", "repair", "generate"]
        self.mode_dropdown = ttk.Combobox(controls_frame, textvariable=self.mode_var, values=modes, state="readonly", width=25)
        self.mode_dropdown.pack(anchor=tk.W, pady=(0, 15))

        # Seed Entry
        ttk.Label(controls_frame, text="Optional Seed (Integer):", style="Card.TLabel").pack(anchor=tk.W, pady=(0, 5))
        self.seed_var = tk.StringVar()
        self.seed_entry = ttk.Entry(controls_frame, textvariable=self.seed_var, width=27)
        self.seed_entry.pack(anchor=tk.W, pady=(0, 25))

        # Run Button
        self.run_button = ttk.Button(controls_frame, text="Run Pipeline", style="Primary.TButton", command=self.start_pipeline_thread)
        self.run_button.pack(fill=tk.X, pady=(0, 10))

        # Clear Log Button
        self.clear_button = ttk.Button(controls_frame, text="Clear Logs", command=self.clear_logs)
        self.clear_button.pack(fill=tk.X)

        # --- RIGHT PANEL: Logs ---
        log_frame = ttk.Frame(content_frame, style="Card.TFrame", padding=1)
        log_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        # Custom text widget styling
        self.log_text = tk.Text(
            log_frame, 
            wrap=tk.WORD, 
            state="disabled", 
            bg="#1e1e1e", 
            fg="#d4d4d4", 
            font=("Consolas", 10),
            padx=10,
            pady=10,
            relief="flat",
            borderwidth=0,
            highlightthickness=0
        )
        self.log_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.configure(yscrollcommand=scrollbar.set)

    def clear_logs(self):
        """Clears the output text window."""
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state="disabled")

    def toggle_ui_state(self, state):
        """Disables or enables UI controls while the pipeline is running."""
        state_str = "normal" if state else "disabled"
        self.mode_dropdown.configure(state="readonly" if state else "disabled")
        self.seed_entry.configure(state=state_str)
        self.run_button.configure(state=state_str)

    def start_pipeline_thread(self):
        """Starts the pipeline execution in a separate thread."""
        self.toggle_ui_state(False)
        self.clear_logs()
        
        mode = self.mode_var.get()
        seed_str = self.seed_var.get().strip()
        
        seed = None
        if seed_str:
            try:
                seed = int(seed_str)
            except ValueError:
                print("[ERROR] Seed must be an integer. Ignoring provided seed.")

        # Run in thread to prevent GUI freezing
        thread = threading.Thread(target=self.run_pipeline_logic, args=(mode, seed), daemon=True)
        thread.start()

    def run_pipeline_logic(self, mode, seed):
        """Executes the actual backend logic."""
        pipeline = UnitstacksPipeline()
        
        try:
            if mode == "validate":
                pipeline.run_validation()
            elif mode == "repair":
                pipeline.run_repair()
            elif mode == "generate":
                pipeline.generate_unitstacks(seed=seed)
            elif mode == "pipeline":
                pipeline.run_pipeline(seed=seed)
        except Exception as e:
            print(f"\n[CRITICAL FAILURE] Pipeline encountered an exception:\n{e}")
        finally:
            print("\n>>> Process finished.")
            # Re-enable the UI from the main thread
            self.after(0, lambda: self.toggle_ui_state(True))

if __name__ == "__main__":
    app = unitstacksApp()
    app.mainloop()
