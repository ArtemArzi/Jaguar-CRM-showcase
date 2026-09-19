import { defineConfig } from "orval";

export default defineConfig({
  jaguar: {
    input: {
      target: "http://localhost:8001/api/openapi.json",
    },
    output: {
      mode: "tags-split",
      target: "src/api/generated",
      schemas: "src/api/generated/models",
      client: "react-query",
      httpClient: "axios",
      clean: true,
      override: {
        mutator: {
          path: "./src/api/custom-fetch.ts",
          name: "customFetch",
        },
      },
    },
  },
});
