<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from 'vue'
import { useChatStore } from '@/stores/chat'
import { useSessionStore } from '@/stores/session'
import { useFileUrls } from '@/composables/useFileUrl'
import { useI18n } from '@/i18n'

const chat = useChatStore()
const session = useSessionStore()
const { t } = useI18n()

/** How many faces fit before the strip would run into the scene panel. */
const LIMIT = 5

const shown = computed(() => chat.cast.slice(0, LIMIT))

const avatarMap = useFileUrls(
  () => shown.value.map((c) => c.avatar).filter((a): a is string => Boolean(a)),
  128,
)

/**
 * Each face is a bead threaded on the wire. `x` is its displacement from rest
 * and `v` its velocity: a spring pulls it home, damping bleeds the energy off,
 * and the pointer pushes it sideways as it passes. Without the spring the beads
 * would simply drift away and never settle.
 */
type Bead = { a: number; v: number }
const beads = ref<Bead[]>([])

function ensure(n: number) {
  while (beads.value.length < n) beads.value.push({ a: 0, v: 0 })
  if (beads.value.length > n) beads.value.length = n
}

let raf = 0

function tick() {
  const list = beads.value
  for (let i = 0; i < list.length; i += 1) {
    const b = list[i]
    b.v += -b.a * 0.0011
    b.v *= 0.9955
    b.a += b.v
  }
  // Neighbours tug on each other in both directions, so a plucked bead pulls
  // the wire either side of it instead of sliding on its own.
  for (let i = 0; i < list.length - 1; i += 1) {
    const pull = (list[i + 1].a - list[i].a) * 0.004
    list[i].v += pull
    list[i + 1].v -= pull
  }
  // Let the cord settle on its own: the loop keeps running after the pointer
  // leaves, and only stops once every pendant is essentially plumb.
  const atRest = list.every((b) => Math.abs(b.a) < 0.03 && Math.abs(b.v) < 0.01)
  if (atRest) {
    for (const b of list) {
      b.a = 0
      b.v = 0
    }
    raf = 0
    return
  }
  raf = requestAnimationFrame(tick)
}

function start() {
  ensure(shown.value.length)
  if (!raf) raf = requestAnimationFrame(tick)
}

function stop() {
  // Deliberately does not cancel the loop: the beads keep coasting and the tick
  // stops itself once they settle.
}

onBeforeUnmount(() => {
  if (raf) cancelAnimationFrame(raf)
})

/** Push a pendant in the direction the pointer is travelling. */
function nudge(i: number, dir: number) {
  const b = beads.value[i]
  if (!b) return
  b.v += dir * 0.1
  // Cap it so a fast flick cannot swing a pendant into its neighbour.
  if (b.v > 0.5) b.v = 0.5
  if (b.v < -0.5) b.v = -0.5
}

function pick(id: string) {
  if (id === session.activeCompanionId) return
  void session.selectCompanion(id)
}
</script>

<template>
  <nav
    v-if="shown.length > 1"
    class="cast-strip"
    :aria-label="t('cast.title')"
    @mouseenter="start"
    @mouseleave="stop"
  >
    <div
      v-for="(c, i) in shown"
      :key="c.id"
      class="cs-bead"
      :style="{ transform: `rotate(${beads[i]?.a ?? 0}deg)` }"
    >
      <span class="cs-string"></span>
      <button
        class="cs-face"
        :class="{ active: c.id === session.activeCompanionId }"
        :data-tip="c.name"
        :aria-label="c.name"
        :aria-current="c.id === session.activeCompanionId ? 'true' : undefined"
        @click="pick(c.id)"
        @mousemove="nudge(i, Math.sign($event.movementX || 0))"
      >
        <img v-if="avatarMap[c.avatar]" :src="avatarMap[c.avatar]" :alt="c.name" />
        <span v-else class="cs-glyph">{{ c.letter }}</span>
      </button>
    </div>
  </nav>
</template>

<style scoped>
.cast-strip{
  position:absolute;
  /* Spans the gap exactly: left edge at the thread column's right edge, right
     edge at the scene panel. Centring inside that span keeps the two outer
     margins equal at any window width. */
  left:calc(var(--gutter) + 720px);
  right:max(300px, 34%);
  top:37px;
  z-index:6;
  display:flex;
  align-items:flex-start;
  justify-content:center;
  gap:8px;
}

/* No crossbar: every face hangs on its own string straight from the top edge,
   the way a set of pendants would be tied off individually. */

.cs-bead{
  position:relative;
  padding-top:44px;
  transform-origin:top center;
  will-change:transform;
}

/* Twine, not a rule. Two pixels wide with a repeating diagonal band, which is
   what the eye reads as the twist of a cord; a warm brown rather than ink so it
   stays distinct from the interface's hairlines. */
.cs-string{
  position:absolute;
  left:50%;top:0;
  width:2px;height:44px;
  transform:translateX(-1px);
  background:
    repeating-linear-gradient(
      108deg,
      rgb(206 176 136 / .78) 0 1px,
      rgb(138 112 80 / .5) 1px 3px
    );
  box-shadow:0.5px 0 0 rgb(var(--shadow-rgb) / .28);
}

.cs-face{
  position:relative;
  width:clamp(38px, 3.4vw, 52px);
  height:clamp(38px, 3.4vw, 52px);
  flex:none;
  margin:0 auto;
  border-radius:50%;
  display:grid;place-items:center;
  opacity:.62;
  filter:grayscale(.45) contrast(.95);
  transition:opacity .38s var(--ease),transform .42s var(--ease),filter .38s ease;
}

.cs-face img{
  width:100%;height:100%;border-radius:50%;object-fit:cover;display:block;
  border:1px solid rgb(var(--ink-rgb) / .14);
  /* Three layers: a wide soft cast that lifts the face off the page, a tight
     contact shadow right under it, and a hairline highlight on the top rim so
     the disc reads as a solid object rather than a flat sticker. */
  box-shadow:
    0 10px 24px rgb(var(--shadow-rgb) / .62),
    0 3px 7px rgb(var(--shadow-rgb) / .48),
    inset 0 1px 0 rgb(var(--ink-rgb) / .1);
  transition:box-shadow .4s var(--ease);
}

.cs-glyph{
  width:100%;height:100%;border-radius:50%;
  display:grid;place-items:center;
  background:var(--bg-3);color:var(--ink-3);
  font-family:var(--serif);font-size:16px;
  border:1px solid rgb(var(--ink-rgb) / .14);
  box-shadow:
    0 10px 24px rgb(var(--shadow-rgb) / .62),
    0 3px 7px rgb(var(--shadow-rgb) / .48),
    inset 0 1px 0 rgb(var(--ink-rgb) / .1);
  transition:box-shadow .4s var(--ease);
}

.cs-face:hover{
  opacity:1;
  filter:none;
  transform:translateY(-3px) scale(1.08);
}

.cs-face.active{
  opacity:1;
  filter:none;
  transform:translateY(-1px);
}

.cs-face.active img,
.cs-face.active .cs-glyph{
  border-color:rgb(var(--accent-rgb) / .85);
  /* Keeps the floating cast and adds the accent ring on top of it. */
  box-shadow:
    0 0 0 2px rgb(var(--accent-rgb) / .6),
    0 10px 24px rgb(var(--shadow-rgb) / .62),
    0 3px 7px rgb(var(--shadow-rgb) / .48),
    inset 0 1px 0 rgb(var(--ink-rgb) / .1);
}

.cs-face.active .cs-glyph{color:var(--accent);}

/* The selected face sits a touch higher and carries an accent ring. The ring
   is a box-shadow so it animates on the same transition as the lift, and it
   settles instead of looping — a permanent pulse reads as "still loading". */
.cs-face.active img,
.cs-face.active .cs-glyph{
  border-color:rgb(var(--accent-rgb) / .85);
  box-shadow:0 0 0 2px rgb(var(--accent-rgb) / .55),
             0 4px 14px rgb(var(--shadow-rgb) / .5);
}

.cs-face.active .cs-glyph{color:var(--accent);}

@media (max-width:1140px){
  .cast-strip{left:28px;top:60px;gap:12px;}
}
</style>
