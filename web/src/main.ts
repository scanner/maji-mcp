/**
 * The web client: the queue of decisions that need you.
 */
import "./styles/fonts.css";
import "./styles/tokens.css";
import "./styles/base.css";

import { createPinia } from "pinia";
import { createApp } from "vue";

import App from "./App.vue";
import { createAppRouter } from "./router";

const pinia = createPinia();
createApp(App).use(pinia).use(createAppRouter(pinia)).mount("#app");
