const heroVisual =
    document.getElementById(
        "heroVisual"
    );


const locationCard =
    document.getElementById(
        "locationCard"
    );


const navLinks =
    document.querySelectorAll(
        ".nav-links a"
    );


const sourceCards =
    document.querySelectorAll(
        ".source-card"
    );


const dataCards =
    document.querySelectorAll(
        ".data-card"
    );


/*
    Small movement effect
    inside the geographic hero.
*/

if (
    heroVisual &&
    locationCard
) {

    heroVisual.addEventListener(
        "mousemove",
        function(event) {

            const box =
                heroVisual
                    .getBoundingClientRect();


            const x =
                (
                    event.clientX -
                    box.left
                )
                /
                box.width;


            const y =
                (
                    event.clientY -
                    box.top
                )
                /
                box.height;


            locationCard.style.transform =
                "translate("
                +
                x * 8
                +
                "px, "
                +
                y * 8
                +
                "px)";

        }
    );


    heroVisual.addEventListener(
        "mouseleave",
        function() {

            locationCard.style.transform =
                "translate(0, 0)";

        }
    );

}


/*
    Navigation active state.
*/

navLinks.forEach(
    function(link) {

        link.addEventListener(
            "click",
            function() {

                navLinks.forEach(
                    function(item) {

                        item.classList.remove(
                            "active"
                        );

                    }
                );


                link.classList.add(
                    "active"
                );

            }
        );

    }
);


/*
    Make the mission rows
    keyboard focusable.
*/

sourceCards.forEach(
    function(card) {

        card.tabIndex = 0;

    }
);


/*
    Add a subtle interactive
    state to the data cards.
*/

dataCards.forEach(
    function(card) {

        card.addEventListener(
            "mouseenter",
            function() {

                card.classList.add(
                    "is-hovered"
                );

            }
        );


        card.addEventListener(
            "mouseleave",
            function() {

                card.classList.remove(
                    "is-hovered"
                );

            }
        );

    }
);