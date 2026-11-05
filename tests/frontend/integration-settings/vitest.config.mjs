import base from '../../../src/frontend/vite.config.js';

export default {
  ...base,
  test: { ...base.test, include: ['../../tests/frontend/integration-settings/settingsPanel.real.jsx'] },
};
