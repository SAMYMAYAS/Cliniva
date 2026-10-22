import { cp, mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";

const projectRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const packageRoot = path.join(projectRoot, "node_modules");
const vendorRoot = path.join(projectRoot, "static", "vendor");

async function copyIconWeight(weight) {
  const sourceDir = path.join(packageRoot, "@phosphor-icons", "web", "src", weight);
  const css = await readFile(path.join(sourceDir, "style.css"), "utf8");
  const fontName = css.match(/url\(["']?(\.[^"')]+\.woff2)/)?.[1];
  if (!fontName) throw new Error(`Police WOFF2 Phosphor introuvable pour ${weight}.`);
  const targetDir = path.join(vendorRoot, "phosphor", weight);
  await mkdir(targetDir, { recursive: true });
  await writeFile(path.join(targetDir, "style.css"), css);
  await cp(path.join(sourceDir, path.basename(fontName)), path.join(targetDir, path.basename(fontName)));
}

await Promise.all(["regular", "bold", "light"].map(copyIconWeight));

await cp(
  path.join(packageRoot, "tinymce"),
  path.join(vendorRoot, "tinymce"),
  { recursive: true },
);
await mkdir(path.join(vendorRoot, "js"), { recursive: true });
await cp(
  path.join(packageRoot, "html2pdf.js", "dist", "html2pdf.bundle.min.js"),
  path.join(vendorRoot, "js", "html2pdf.bundle.min.js"),
);

console.log("Ressources hors ligne mises à jour dans static/vendor.");
