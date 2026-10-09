# Roadmap

FolioScribe is a development preview. The local annotation viewer and synthetic training example are usable; the following work is still open.

## Annotation to training

Build an explicit adapter from reviewed annotation revisions to SFT inputs. Record the selected annotator and revision, acceptance policy, type mapping, coordinate conversions and cross-page relationships. Keep train/test splits separate at the document level and test content conservation end to end.

## Reliable document state

Extend table continuation coverage to merged cells, incompatible columns and multiple concurrent continuation chains. Define strict handling for malformed DSL and evaluate relationships with block-level matching rather than page/type counts alone.

## Viewer collaboration

Add optimistic revision checks, clear conflict recovery and persistent local drafts. Integrate model predictions with the main annotation viewer after defining sample/page/block alignment.

## Maintainable modules

Extract PDF reading, candidate fusion, table geometry and Word binding from the evidence builder. Split viewer API, navigation, editing and table behavior into smaller modules. Preserve behavior with existing regression tests before changing algorithm thresholds.

## Reproducible training and benchmarks

Provide versioned training launchers and tested GPU environments. Establish a shareable document benchmark with hardware, page counts, stage timings, peak memory and accuracy definitions. CPU projection benchmarks are available in `benchmarks/`; they do not measure model inference or document accuracy.
