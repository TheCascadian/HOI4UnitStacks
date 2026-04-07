<img width="3128" height="213" alt="fontbolt" src="https://github.com/user-attachments/assets/f335222a-2225-4c72-9740-94d3ac658d1e" />


<div align="center">

<img width="1633" alt="HOI4UnitStacks Preview" src="https://github.com/user-attachments/assets/82faeb5e-fad6-42d2-aafa-b5010673fcc6" />

<br/>

![Python](https://img.shields.io/badge/Python-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Cython](https://img.shields.io/badge/Cython-C_Extension-FFD43B?style=for-the-badge)
![Tkinter](https://img.shields.io/badge/GUI-Tkinter-informational?style=for-the-badge)
![HOI4](https://img.shields.io/badge/Hearts_of_Iron_IV-Modding-8B0000?style=for-the-badge)

*A fast, accessible generator for Hearts of Iron IV `unitstacks.txt` files. Written in Python, compiled to C via Cython for performance-critical workloads.*

</div>

---

## Overview

HOI4UnitStacks automates the generation of `unitstacks.txt`, a map data file required by Hearts of Iron IV mods. The tool is distributed as a Tkinter desktop application for broad accessibility, with a Cython-compiled C extension path available for users who need maximum throughput.

> **Before compiling:** The generated C source is substantial. Review (or at minimum skim) the `.pyx` file before running `cythonize`. This is not optional advice.

---

## Performance

On capable hardware (modern multi-core CPU, high-speed RAM), generation is fast. Results will vary. If you encounter slowdowns, incorrect output, or any unexpected behavior, **open an issue immediately** -- targeted refinement is a priority.

---

## Authorship and Attribution

This repository is the **only** canonical source for this version of the script.

- **Author:** wrekt ([TheCascadian](https://github.com/TheCascadian))
- **Inspired by:** [greenbueller](https://github.com/greenbueller) of the HOI4 Modding Den Discord
- **Original script:** [hoi4-modding-den/Dens-HRE](https://github.com/hoi4-modding-den/Dens-HRE/blob/main/map/unitstacks.py)

---

## Known Caveats

Testing has been thorough, but real-world validation is limited by my (wrekt/TheCascadian) lack of familiarity with `unitstacks` in active modding contexts. **Do not assume it works until you have confirmed zero errors in-game.** It may turn out to be unremarkable -- or it may save you hours. Test before committing to it.

---

## Troubleshooting

If the output does not behave as expected in-game, work through the following before opening an issue:

**Input file issues are the most common cause of failure.**

Verify your source files against vanilla HOI4 game files on three points:

1. **Formatting** -- the structure must match vanilla exactly, including the first line of each file (historically a source of subtle breakage)
2. **Encoding** -- files must use the same character encoding as the vanilla originals
3. **Line endings** -- the generator is sensitive to line ending style; ensure your files use the correct format for your environment

If all three check out and the issue persists, open an issue with as much detail as possible.

---

## Confirmation

If the tool works correctly for your mod, please confirm it on the [HOI4 Modding Den Discord](https://discord.gg/XXqQwm96eY). Ping **wrekt** directly -- all messages are seen and responded to as soon as possible. Positive confirmations help scope the tool's reliability across different mod setups.

---

<div align="center">

*Repository maintained by [TheCascadian](https://github.com/TheCascadian)*

</div>
