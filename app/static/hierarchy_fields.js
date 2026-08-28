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
    const hasPartAxis = isSeason || isPart;
    const isConcrete = Boolean(partType);

    setFieldState(form, "[data-season-number]", isConcrete);
    setFieldState(form, "[data-season-label]", isConcrete && !isPart);
    setFieldState(form, "[data-part-number]", hasPartAxis);

    const partInput = form.querySelector('input[name="part_number_manual"], input[name="part_number"]');
    if (partInput) partInput.required = isPart;

    const save = form.querySelector(".manual-hierarchy-save");
    if (save) save.disabled = !partType;
  }

  document.querySelectorAll(".structural-fields-form, .manual-hierarchy-form").forEach(function (form) {
    updateStructuralForm(form);
    const typeSelect = form.querySelector('select[name="part_type_manual"], select[name="part_type"]');
    typeSelect.addEventListener("change", function () {
      updateStructuralForm(form);
    });
  });
})();
