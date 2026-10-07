import { useEffect, useState } from "react";
import type { components } from "../../api/schema";
import { listTrainingRecipes } from "../../api/training";
export function RecipePicker({ onSelect }: { onSelect: (recipe: components["schemas"]["TrainingRecipe"]) => void }) {
  const [recipes, setRecipes] = useState<components["schemas"]["TrainingRecipe"][] | null>(null);
  const [error, setError] = useState("");
  useEffect(() => { let live = true; listTrainingRecipes().then((page) => { if (live) setRecipes(page.items); }).catch((e) => { if (live) setError(`Recipe catalog unavailable: ${String(e)}`); }); return () => { live = false; }; }, []);
  return <section aria-label="Training recipes"><h3>Versioned recipes</h3>
    {error ? <p role="status">{error}</p> : recipes === null ? <p role="status">Loading recipes…</p> : recipes.length === 0 ? <p>No recipes are available.</p> : recipes.map((recipe) => <button className="button ghost" type="button" key={recipe.id} onClick={() => onSelect(recipe)}>{recipe.id} · v{recipe.version} · {recipe.parameterization}</button>)}
    <p>Templates require real registry model, variant, dataset and output bindings. A recipe name does not prove runtime support.</p>
  </section>;
}
