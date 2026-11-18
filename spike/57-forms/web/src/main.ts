import "@picocss/pico/css/pico.min.css";
import "@sjsf/basic-theme/css/pico.css";
import "./app.css";
import { mount } from "svelte";
import App from "./App.svelte";

mount(App, { target: document.getElementById("app")! });
