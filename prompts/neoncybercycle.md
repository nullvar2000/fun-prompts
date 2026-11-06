Write a complete, single-file HTML5 game called "Neon Neon Cyber-Rider". The game must be entirely self-contained inside a single `index.html` file using standard HTML, CSS, and vanilla JavaScript (Canvas 2D API). Do not use any external CDNs, frameworks, external images, or audio assets—all graphics must be rendered procedurally using Canvas drawing methods.

Game Concept & Mechanics:
- It is a fast-paced, endless 2D side-scrolling obstacle avoidance game.
- The player controls a neon-glowing cyber-cycle. The cycle automatically moves forward, but the player can press the UP arrow (or click/tap) to jump, and the DOWN arrow to slide/duck.
- Obstacles spawn from the right side of the screen and move left: low barries (must jump over), high barriers (must slide under), and mid-air neon drones (can jump over or slide under).
- The game speed should gradually increase over time to ramp up the difficulty.
- Implement a scoring system based on distance traveled (survival time), plus a "high score" saved to localStorage so it persists across refreshes.

Visual & Audio Style:
- Use a high-contrast Cyberpunk / Synthwave aesthetic: deep black/dark violet background with glowing neon pink (#ff007f), cyan (#00ffff), and violet elements.
- Implement a parallax background effect (e.g., distant neon skyscrapers moving slower than the foreground).
- Use canvas shadowBlur and shadowColor to create authentic neon glowing effects on the player and obstacles.
- Include a simple sound synth using the Web Audio API (no external audio files) to play a crisp "beep" when jumping and an explosive "zap" sound if the player hits an obstacle and loses.

UI and Game Loop:
- Structure the game loop with requestAnimationFrame.
- Include a sleek "Start Game" overlay screen with a title and instructions.
- If the player hits an obstacle, transition to a "Game Over" screen showing their final score, their all-time high score, and a "Restart" button.
- Make the canvas responsive so it handles resizing gracefully or centers cleanly on screen.

Deliver only the raw HTML code block. Ensure it contains absolutely no placeholder comments, missing logic, or "TODO" sections. Every function must be fully written, syntactically correct, and playable the moment the file is saved and opened in a browser.
