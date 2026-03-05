import { createApp } from 'vue';
import { createVuetify } from 'vuetify';
import * as components from 'vuetify/components';
import * as directives from 'vuetify/directives';
import { DynamicFormsVueGrid } from '@dynamicforms/vue-grid';
import VueMarkdown from 'vue-markdown-render';

import 'vuetify/dist/vuetify.css';
import '@dynamicforms/vuetify-inputs/styles.css';
import '@dynamicforms/vue-grid/styles.css';

import App from './App.vue';

const vuetify = createVuetify({
  components,
  directives,
  theme: { defaultTheme: 'dark' },
});

const app = createApp(App);
app.use(vuetify);
app.use(DynamicFormsVueGrid, { registerComponents: true });
app.component('VueMarkdown', VueMarkdown);
app.mount('#app');
