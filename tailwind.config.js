/** Build-time Tailwind configuration for the offline Cliniva interface. */
module.exports = {
  content: [
    "./templates/**/*.html",
    "./modules/**/*.py",
  ],
  darkMode: "class",
  theme: {
    extend: {
      fontFamily: {
        sans: ["Segoe UI", "Arial", "sans-serif"],
      },
      colors: {
        brand: {
          neon: "#8be140",
          neonDark: "#75c832",
          darkBg: "#0b160f",
          cardDark: "#121c15",
          cardBorder: "rgba(255, 255, 255, 0.08)",
        },
      },
    },
  },
  safelist: [
    "hidden", "flex", "grid", "block", "inline-flex",
    "bg-brand-neon", "text-brand-neon", "border-brand-neon",
  ],
  plugins: [],
};
