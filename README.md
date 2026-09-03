# hello-world
For COP 4655

I am a senior at FAU

## Human Brain Simulator

An interactive brain simulator in a single `index.html` (no build step, no
dependencies). Open it in a browser.

- **Explore** — click any region in the *Outside* (lateral) or *Inside*
  (medial) view. The panel shows what it does, what happens when it is
  damaged, and a fact. **Stimulate** fires the region: it lights up, neurons
  spark inside it, the activity monitor switches to that region's rhythm, and
  an output log reports what the body does.
- **Scenarios** — "Catching a ball", "A sudden loud bang", "Falling asleep"...
  fire regions in sequence so you can watch the brain work as a system. The
  view switches automatically as the sequence moves between surface and deep
  structures.
- **Quiz** — "Click the region that keeps your heart beating while you sleep."

Regions covered: frontal, parietal, temporal and occipital lobes, cerebellum,
brainstem, corpus callosum, thalamus, hypothalamus, pituitary gland,
hippocampus, amygdala.

### How it is built

Three registries at the bottom of `index.html` define everything:

| Registry    | What it holds                                                     |
| ----------- | ----------------------------------------------------------------- |
| `VIEWS`     | The drawings you can switch between (each is one SVG in the page) |
| `REGIONS`   | Every clickable structure: text, colour, rhythm, quiz clue        |
| `SCENARIOS` | Ordered lists of regions firing, with a caption per step          |

The legend, info panel, activity monitor, scenario list and quiz are all
generated from these — nothing is hard-coded in the UI.

### Add a brain region

1. Draw it as `<path data-region="broca" style="--rc:#hex" d="..."/>` inside
   the SVG for the view it belongs to.
2. Add a `REGIONS` entry with `id: "broca"` and the same `view`.
3. Optionally reference it from a scenario step: `["broca", 1200, "caption"]`.

### Add a different body part (heart, lungs, eye...)

The same pattern scales past the brain:

1. Add `{ id: "heart", label: "Heart", title: "..." }` to `VIEWS`.
2. Add an `<svg class="view" data-view="heart" viewBox="...">` next to the
   brain SVGs, with `data-region` shapes for the atria, ventricles, valves...
3. Add `REGIONS` entries with `view: "heart"`.
4. Scenarios can now cross organs: an "Exercise" scenario could fire the
   motor cortex, then the brainstem, then the heart's sinoatrial node.
