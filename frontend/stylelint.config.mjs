export default {
  ignoreFiles: ["dist/**", "src/styles/palette.css"],
  rules: {
    "color-no-hex": [true, { message: "Use a palette or semantic color token instead of a raw hex value." }],
  },
};
