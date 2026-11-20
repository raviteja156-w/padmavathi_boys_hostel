// Rebuild static/css/tailwind.css whenever you add NEW Tailwind classes to a template or JS file:
//   cd tools/tailwind && npm i tailwindcss@3.4.17 && npx tailwindcss -c tailwind.config.js -i in.css -o ../../static/css/tailwind.css --minify
module.exports = {
  content: ['../../templates/**/*.html', '../../static/js/**/*.js', '../../app.py', '../../assistant.py', '../../agent.py'],
  theme: { extend: {
    colors: { primary: { DEFAULT: '#1E3A8A', light: '#3B82F6', dark: '#152a63' }, accent: '#3B82F6', success: '#16A34A', danger: '#DC2626' },
    fontFamily: { sans: ['Inter', 'system-ui', 'sans-serif'] },
    animation: { 'fade-in': 'fadeIn .15s ease-out', 'slide-up': 'slideUp .2s ease-out', 'pop': 'pop .2s ease-out' },
    keyframes: {
      fadeIn: { '0%': { opacity: 0 }, '100%': { opacity: 1 } },
      slideUp: { '0%': { opacity: 0, transform: 'translateY(12px)' }, '100%': { opacity: 1, transform: 'translateY(0)' } },
      pop: { '0%': { transform: 'scale(.85)', opacity: 0 }, '100%': { transform: 'scale(1)', opacity: 1 } } } } },
};
