import type { CodegenConfig } from "@graphql-codegen/cli";

// Generates typed documents from operations in src/ and validates them against the
// exported schema (docs/schema.graphql). CI runs `npm run codegen:check`.
const config: CodegenConfig = {
  schema: "../docs/schema.graphql",
  documents: ["src/**/*.{ts,tsx}", "!src/gql/**"],
  ignoreNoDocuments: false,
  generates: {
    "./src/gql/": {
      preset: "client",
      presetConfig: { fragmentMasking: false },
      config: {
        scalars: { DateTime: "string" },
        enumsAsTypes: true,
        avoidOptionals: { field: true, inputValue: false },
        defaultScalarType: "unknown",
        useTypeImports: true,
      },
    },
  },
};
export default config;
