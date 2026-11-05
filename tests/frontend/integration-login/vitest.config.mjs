import base from '../../../src/frontend/vite.config.js';

export default {
  ...base,
  test: { ...base.test, environmentOptions: { jsdom: { url: 'http://127.0.0.1:4173/' } },
    include: ['../../tests/frontend/integration-login/*.real.jsx'], testTimeout: 90000 },
};
