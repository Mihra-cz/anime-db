(function () {
  function setFieldState(container, selector, visible) {
    container.querySelectorAll(selector).forEach(function (field) {
      field.hidden = !visible;
      field.querySelectorAll("input").forEach(function (input) {
        input.disabled = !visible;
      });
    });
  }

  function updateStructuralForm(form) {
    const typeSelect = form.querySelector('select[name="part_type_manual"], select[name="part_type"]');
    if (!typeSelect) return;
    const partType = typeSelect.value;
    const isSeason = partType === "season";
    const isPart = partType === "part";
    // Legacy ``cour`` is only rendered for a title that already stores it; it
    // keeps its Part number exactly like title_hierarchy_conditional_fields().
    const hasPartAxis = isSeason || isPart || partType === "cour";
    const isConcrete = Boolean(partType);

    setFieldState(form, "[data-season-number]", isConcrete);
    setFieldState(form, "[data-season-label]", isConcrete && !isPart);
    setFieldState(form, "[data-part-number]", hasPartAxis);

    const partInput = form.querySelector('input[name="part_number_manual"], input[name="part_number"]');
    if (partInput) partInput.required = isPart;

    const save = form.querySelector(".manual-hierarchy-save");
    if (save) save.disabled = !partType;
  }

  function updateVideoNumberField(form) {
    const typeSelect = form.querySelector('select[name="content_type"]');
    const label = form.querySelector('[data-number-field-label]');
    const input = form.querySelector('input[name="manual_episode_number"]');
    if (!typeSelect || !label || !input) return;
    const option = typeSelect.options[typeSelect.selectedIndex];
    label.textContent = option.dataset.numberLabel || "Ruční ordinal";
    const recap = typeSelect.value === "recap" || (
      !typeSelect.value && label.textContent === "Ruční pozice Recapu"
    );
    input.step = recap ? "0.1" : "1";
    input.inputMode = recap ? "decimal" : "numeric";
  }

  document.querySelectorAll(".structural-fields-form, .manual-hierarchy-form").forEach(function (form) {
    updateStructuralForm(form);
    const typeSelect = form.querySelector('select[name="part_type_manual"], select[name="part_type"]');
    typeSelect.addEventListener("change", function () {
      updateStructuralForm(form);
    });
  });
  document.querySelectorAll(".hierarchy-video-editor").forEach(function (form) {
    updateVideoNumberField(form);
    form.querySelector('select[name="content_type"]').addEventListener("change", function () {
      updateVideoNumberField(form);
    });
  });
})();
