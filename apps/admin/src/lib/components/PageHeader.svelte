<script lang="ts">
let {
  eyebrow = "OPERATIONS",
  title,
  description,
} = $props<{ eyebrow?: string; title: string; description: string }>();
</script>

<header class="page">
  <p>{eyebrow}</p>
  <h1>{title}</h1>
  <span>{description}</span>
</header>

<style>
  .page {
    position: static;
    display: block;
    padding: 0 0 20px;
    border: 0;
    background: transparent;
  }
  .page p {
    margin: 0;
    color: #91cb3f;
    font-size: 0.75rem;
    font-weight: 900;
    letter-spacing: 0.13em;
  }
  .page h1 {
    margin: 6px 0;
    font-size: clamp(1.8rem, 4vw, 3rem);
  }
  .page span {
    color: #b5bdb9;
    line-height: 1.6;
  }
</style>
