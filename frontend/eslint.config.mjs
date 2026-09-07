import { defineConfig, globalIgnores } from "eslint/config";
import nextVitals from "eslint-config-next/core-web-vitals";
import nextTypescript from "eslint-config-next/typescript";

const eslintConfig = defineConfig([
  ...nextVitals,
  ...nextTypescript,
  globalIgnores([".next/**", "out/**", "build/**", "next-env.d.ts"]),
  {
    rules: {
      // Allow `any` in limited cases (e.g. Recharts tooltip props)
      "@typescript-eslint/no-explicit-any": "warn",
      // Allow non-null assertions for known-safe patterns (e.g. clamped_from!)
      "@typescript-eslint/no-non-null-assertion": "off",
    },
  },
]);

export default eslintConfig;
