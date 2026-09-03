# hello-world
For COP 4655

I am a senior at FAU

## Human Brain Simulator

An interactive **3D** brain simulator in a single `index.html`. The brain is
generated procedurally with [Three.js](https://threejs.org) (loaded from a
CDN — no model files, no build step). Open it in a browser.

- **Rotate and zoom** — drag to orbit, scroll to zoom. The brain auto-rotates
  when idle. Labels float over each region and follow it as it turns.
- **Explore** — click any region on the mesh. *Surface* shows the wrinkled
  cortex, cerebellum and brainstem; *Inside* makes the cortex translucent to
  reveal the thalamus, hippocampus, amygdala, corpus callosum and more.
  The panel shows what the region does, what happens when it is damaged, and
  a fact. **Stimulate** fires it: the region glows and pulses, neurons spark
  out of its surface, the activity monitor switches to that region's rhythm,
  and an output log reports what the body does.
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
| `VIEWS`     | Surface (opaque cortex) and Inside (translucent cortex)           |
| `REGIONS`   | Every clickable structure: text, colour, rhythm, quiz clue        |
| `SCENARIOS` | Ordered lists of regions firing, with a caption per step          |

The legend, info panel, activity monitor, scenario list and quiz are all
generated from these — nothing is hard-coded in the UI.

### How the 3D brain is made

- Each hemisphere is a high-resolution sphere, squashed into an ellipsoid and
  displaced along its surface with two octaves of ridged simplex noise —
  that is what makes the gyri and sulci. A smooth mask bulges the temporal
  lobe outward.
- Every cortex vertex is classified into a lobe by its position (front /
  back / top / lower-side), stored as a per-vertex attribute, and coloured
  from the registry. Clicking the mesh raycasts to a face and reads that
  attribute back, so picking is exact.
- Cerebellum, brainstem and the deep structures are separate meshes
  (ellipsoids, a tapered cylinder, tube geometry swept along curves) tagged
  with their region id.

### Add a brain region

- **A new deep structure:** call `addRegionMesh("basalganglia", geometry,
  { position, scale, deep: true })` next to the other deep structures and add
  a `REGIONS` entry with `view: "medial"`.
- **A new cortical area** (e.g. Broca's area): extend the vertex classifier
  in `buildHemisphere` with a rule for the patch of surface it covers, add its
  id to `cortexRegions`, and add the `REGIONS` entry.
- Optionally reference it from a scenario step: `["broca", 1200, "caption"]`.

### Add a different body part (heart, lungs, eye...)

The same pattern scales past the brain: build the organ's meshes with
`addRegionMesh(...)` under a new `VIEWS` entry, add `REGIONS` with that view,
and hide/show the groups in `setView`. Scenarios can then cross organs: an
"Exercise" scenario could fire the motor cortex, then the brainstem, then the
heart's sinoatrial node.
