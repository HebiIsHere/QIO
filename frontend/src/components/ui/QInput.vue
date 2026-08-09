<script setup lang="ts">
defineProps<{ modelValue?: string; placeholder?: string; error?: boolean; mono?: boolean; disabled?: boolean; type?: string }>();
defineEmits<{ "update:modelValue": [string] }>();
</script>
<template>
  <input
    class="qio-input" :class="{ err: error, mono }" :type="type ?? 'text'"
    :value="modelValue" :placeholder="placeholder" :disabled="disabled"
    @input="$emit('update:modelValue', ($event.target as HTMLInputElement).value)"
  />
</template>
