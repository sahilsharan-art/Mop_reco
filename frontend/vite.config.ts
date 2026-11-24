import { defineConfig } from 'vite';
import { tanstackStart } from '@tanstack/react-start/plugin/vite';
import react from '@vitejs/plugin-react';
import tailwind from '@tailwindcss/vite';
import tsconfigPaths from 'vite-tsconfig-paths';
export default defineConfig({ plugins: [tsconfigPaths(), tailwind(), tanstackStart(), react()], server: { proxy: { '/api/mop': { target: 'http://127.0.0.1:8092', changeOrigin: false } } } });
