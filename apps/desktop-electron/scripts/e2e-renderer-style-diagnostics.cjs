"use strict";

// This module deliberately keeps renderer evidence small: it is intended for a
// failing E2E artifact, not for exporting page or stylesheet contents.
const MAX_STYLESHEETS = 32;
const MAX_RULES = 48;
const MAX_CSS_TEXT = 640;
const MAX_CHAIN = 6;

async function collectRendererStyleDiagnostics(page) {
  if (!page || typeof page.evaluate !== "function") {
    throw new TypeError("collectRendererStyleDiagnostics requires a CDP page with evaluate()");
  }

  return page.evaluate(`(() => {
    const MAX_STYLESHEETS = ${MAX_STYLESHEETS};
    const MAX_RULES = ${MAX_RULES};
    const MAX_CSS_TEXT = ${MAX_CSS_TEXT};
    const MAX_CHAIN = ${MAX_CHAIN};
    const targetSelector = 'nav[aria-label="主导航"] a.active';
    const target = document.querySelector(targetSelector)
      || document.querySelector('.rebuild-home-sidebar nav a.active');
    const clip = (value, max = MAX_CSS_TEXT) => {
      const text = String(value || '');
      return text.length <= max ? text : text.slice(0, max) + '…';
    };
    const classSummary = (node) => node instanceof Element
      ? { tag: node.tagName.toLowerCase(), id: node.id || null, className: clip(node.className || '', 240) }
      : null;

    if (!(target instanceof HTMLElement)) {
      return { targetSelector, target: null, stylesheets: [], matchingRules: [], note: 'active navigation target unavailable' };
    }

    const targetStyle = getComputedStyle(target);
    const chain = [];
    for (let node = target; node instanceof Element && chain.length < MAX_CHAIN; node = node.parentElement) {
      chain.push(classSummary(node));
    }

    const matchingRules = [];
    const stylesheetSummaries = [];
    const relevantDeclaration = (style) => {
      if (!style) return false;
      return ['color', 'background', 'background-color', '--seal-red', '--ruby-2']
        .some((property) => style.getPropertyValue(property));
    };
    const conditionsApply = (conditions) => conditions.every((condition) => condition.applies);
    const addRule = (rule, sheetIndex, path, conditions) => {
      if (matchingRules.length >= MAX_RULES || !rule.selectorText || !relevantDeclaration(rule.style)) return;
      let matches = false;
      try { matches = target.matches(rule.selectorText); } catch { return; }
      if (!matches) return;
      matchingRules.push({
        stylesheet: sheetIndex,
        path,
        selector: clip(rule.selectorText, 360),
        conditions,
        conditionsApply: conditionsApply(conditions),
        declarations: {
          color: rule.style.getPropertyValue('color').trim() || null,
          background: rule.style.getPropertyValue('background').trim() || null,
          backgroundColor: rule.style.getPropertyValue('background-color').trim() || null,
          sealRed: rule.style.getPropertyValue('--seal-red').trim() || null,
          ruby2: rule.style.getPropertyValue('--ruby-2').trim() || null,
        },
        cssText: clip(rule.cssText),
      });
    };
    const walkRules = (rules, sheetIndex, path = [], conditions = []) => {
      if (!rules || matchingRules.length >= MAX_RULES) return;
      for (let index = 0; index < rules.length && matchingRules.length < MAX_RULES; index += 1) {
        const rule = rules[index];
        const nextPath = path.concat(index);
        if (rule.type === CSSRule.STYLE_RULE) {
          addRule(rule, sheetIndex, nextPath, conditions);
        } else if (rule.type === CSSRule.MEDIA_RULE) {
          const query = rule.media.mediaText;
          walkRules(rule.cssRules, sheetIndex, nextPath, conditions.concat({ type: 'media', query: clip(query, 240), applies: matchMedia(query).matches }));
        } else if (rule.type === CSSRule.SUPPORTS_RULE) {
          const condition = rule.conditionText;
          walkRules(rule.cssRules, sheetIndex, nextPath, conditions.concat({ type: 'supports', condition: clip(condition, 240), applies: CSS.supports(condition) }));
        } else if (rule.cssRules) {
          // @layer and other grouping rules have no conditional predicate here;
          // retaining their type in the path prevents treating nested rules as root rules.
          walkRules(rule.cssRules, sheetIndex, nextPath, conditions.concat({ type: rule.constructor?.name || 'group', applies: true }));
        }
      }
    };

    Array.from(document.styleSheets).slice(0, MAX_STYLESHEETS).forEach((sheet, sheetIndex) => {
      const owner = sheet.ownerNode;
      const summary = {
        index: sheetIndex,
        href: sheet.href || null,
        owner: owner instanceof Element ? owner.tagName.toLowerCase() : null,
        media: sheet.media?.mediaText || null,
        accessible: false,
        ruleCount: null,
      };
      try {
        const rules = sheet.cssRules;
        summary.accessible = true;
        summary.ruleCount = rules.length;
        walkRules(rules, sheetIndex);
      } catch (error) {
        summary.error = clip(error?.name || 'cssom_unavailable', 80);
      }
      stylesheetSummaries.push(summary);
    });

    return {
      targetSelector,
      target: {
        ...classSummary(target),
        inlineStyle: clip(target.getAttribute('style') || '', 360) || null,
        computed: {
          color: targetStyle.color,
          background: targetStyle.background,
          backgroundColor: targetStyle.backgroundColor,
          sealRed: getComputedStyle(document.documentElement).getPropertyValue('--seal-red').trim() || null,
          ruby2: getComputedStyle(document.documentElement).getPropertyValue('--ruby-2').trim() || null,
        },
        parentChain: chain,
      },
      theme: document.documentElement.dataset.theme || null,
      stylesheets: stylesheetSummaries,
      matchingRules,
      matchingRulesTruncated: matchingRules.length >= MAX_RULES,
    };
  })()`);
}

module.exports = { collectRendererStyleDiagnostics };
