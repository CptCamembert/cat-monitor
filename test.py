import os
from PIL import Image
import numpy as np
import matplotlib.pyplot as plt

APP_DIR = os.path.dirname(__file__)
dark_dir = os.path.join(APP_DIR, "Images", "dark")
bright_dir = os.path.join(APP_DIR, "Images", "bright")


def remove_similar_images(folder: str, percentiles: range, max_mean_diff: float = 2.0) -> None:
  """Remove images whose percentile brightness curve is within max_mean_diff
  (average per-percentile difference, 0-255 scale) of an already-kept image."""
  filenames = sorted(os.listdir(folder))
  vectors = []
  for filename in filenames:
    image = Image.open(os.path.join(folder, filename)).convert("L")
    pixels = np.array(image)
    vectors.append([np.percentile(pixels, p) for p in percentiles])
  vectors = np.array(vectors)

  kept_indices = []
  removed_count = 0
  for i, vec in enumerate(vectors):
    is_redundant = any(
        np.mean(np.abs(vec - vectors[j])) < max_mean_diff
        for j in kept_indices
    )
    if is_redundant:
      os.remove(os.path.join(folder, filenames[i]))
      removed_count += 1
    else:
      kept_indices.append(i)

  print(f"{folder}: removed {removed_count} of {len(filenames)} images, kept {len(kept_indices)}")


def load_percentile_values(folder: str, percentiles: range) -> np.ndarray:
  """Returns an (n_images, n_percentiles) array of brightness values."""
  values = []
  for filename in os.listdir(folder):
    image_path = os.path.join(folder, filename)
    image = Image.open(image_path).convert("L")
    pixels = np.array(image)
    values.append([np.percentile(pixels, p) for p in percentiles])
  return np.array(values)


def weighted_threshold(dark_values: np.ndarray, bright_values: np.ndarray) -> float:
  """Variance-weighted midpoint between the two classes' means. Pulls the
  boundary toward whichever class is more tightly clustered, instead of
  brute-forcing a split that happens to perfectly separate the sample."""
  dark_mean, dark_std = np.mean(dark_values), np.std(dark_values)
  bright_mean, bright_std = np.mean(bright_values), np.std(bright_values)
  if dark_std + bright_std == 0:
    return (dark_mean + bright_mean) / 2.0
  return (dark_std * bright_mean + bright_std * dark_mean) / (dark_std + bright_std)


def best_threshold(dark_values: np.ndarray, bright_values: np.ndarray):
  """Given 1D arrays of dark/bright values for one percentile, derive a
  threshold from class statistics and score it with balanced accuracy.
  Returns (threshold, balanced_accuracy, margin)."""
  thresh = weighted_threshold(dark_values, bright_values)

  dark_correct = np.mean(dark_values < thresh)
  bright_correct = np.mean(bright_values >= thresh)
  balanced_acc = (dark_correct + bright_correct) / 2.0

  dark_mean = np.mean(dark_values)
  bright_mean = np.mean(bright_values)
  margin = min(thresh - dark_mean, bright_mean - thresh)

  return thresh, balanced_acc, margin


percentiles = range(1, 100, 1)
remove_similar_images(dark_dir, percentiles, max_mean_diff=5.0)
remove_similar_images(bright_dir, percentiles, max_mean_diff=5.0)
dark_matrix = load_percentile_values(dark_dir, percentiles)
bright_matrix = load_percentile_values(bright_dir, percentiles)

print(f"{'percentile':>10} {'threshold':>10} {'bal_acc':>10} {'margin':>10}")
best_overall = None
for i, p in enumerate(percentiles):
  thresh, acc, margin = best_threshold(dark_matrix[:, i], bright_matrix[:, i])
  print(f"{p:>10} {thresh:>10.2f} {acc:>10.3f} {margin:>10.2f}")
  if best_overall is None or acc > best_overall[1] or (acc == best_overall[1] and margin > best_overall[2]):
    best_overall = (p, acc, margin, thresh)

print()
print(f"Best: percentile={best_overall[0]}, threshold={best_overall[3]:.2f}, "
      f"balanced_accuracy={best_overall[1]:.3f}, margin={best_overall[2]:.2f}")

threshes, accs, margins = [], [], []
for i, p in enumerate(percentiles):
  thresh, acc, margin = best_threshold(dark_matrix[:, i], bright_matrix[:, i])
  threshes.append(thresh)
  accs.append(acc)
  margins.append(margin)

fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(8, 11), sharex=True)
ax1.plot(list(percentiles), accs, label="balanced accuracy")
ax1.axvline(best_overall[0], color="red", linestyle="--", label="best percentile")
ax1.set_ylabel("balanced accuracy")
ax1.legend()

ax2.plot(list(percentiles), threshes, label="threshold")
ax2.axvline(best_overall[0], color="red", linestyle="--")
ax2.set_ylabel("threshold")
ax2.legend()

ax3.plot(list(percentiles), margins, label="margin")
ax3.axvline(best_overall[0], color="red", linestyle="--")
ax3.set_xlabel("percentile")
ax3.set_ylabel("margin")
ax3.legend()

plt.tight_layout()
plt.savefig(os.path.join(APP_DIR, "threshold_analysis.png"))
plt.show()

best_p_index = best_overall[0] - 1  # percentiles start at 1
plt.figure(figsize=(6, 4))
plt.hist(dark_matrix[:, best_p_index], bins=20, alpha=0.6, label="dark")
plt.hist(bright_matrix[:, best_p_index], bins=20, alpha=0.6, label="bright")
plt.axvline(best_overall[3], color="red", linestyle="--", label="threshold")
plt.xlabel(f"brightness (percentile {best_overall[0]})")
plt.legend()
plt.show()