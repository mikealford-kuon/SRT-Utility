import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  return {
    base: env.VITE_SERVER_BASE || "/",
    plugins: [react()],
    server: {
      port: 5173,
      host: process.env.APP_HOST || "0.0.0.0",
    },
  };
});
