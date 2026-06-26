import { writable } from 'svelte/store'
import type { Cat, BoxHealth, Bowl, Feeder } from './api'

export const cats = writable<Cat[]>([])
export const box = writable<BoxHealth | null>(null)
export const bowls = writable<Bowl[]>([])
export const feeders = writable<Feeder[]>([])
export const state = writable<any>(null)
export const connected = writable<boolean>(false)
