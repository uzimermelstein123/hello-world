# hello-world
For COP 4655

I am a senior at FAU

## Body Parts Buddy

An interactive body-parts animation: **Bo**, a cartoon character whose body
parts each animate when you tap them (wave arms, wiggle ears, boop the nose,
stomp feet...). It has two modes:

- **Explore** — tap any part; Bo animates it, announces it in a speech
  bubble, and says it out loud (Web Speech API).
- **Find it!** — a quiz: "Where is my tummy?" Tap the right part to score.

Open `index.html` in a browser — no build step, no dependencies.

### How to add a new body part

Everything is driven by the `PARTS` registry in `index.html`. Three steps:

1. **Draw it** — add a group inside the SVG:
   ```html
   <g data-part="knees" tabindex="0" role="button" aria-label="Knees">
     <circle cx="179" cy="452" r="10" fill="var(--sun)"/>
     <circle cx="241" cy="452" r="10" fill="var(--sun)"/>
   </g>
   ```
2. **Register it** — add one entry to the `PARTS` array (the `id` must match
   `data-part`):
   ```js
   { id: "knees", label: "Knees", anim: "bend", say: "I bend my knees!" },
   ```
3. **Animate it** — add a CSS class named `anim-<your anim>`:
   ```css
   .anim-bend { animation: bend 0.8s ease; transform-origin: 210px 452px; }
   @keyframes bend {
     0%, 100% { transform: translateY(0); }
     50% { transform: translateY(-14px); }
   }
   ```

The part chip, the quiz round, the highlight, and the voice line are all
generated from the registry automatically.
