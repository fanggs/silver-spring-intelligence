const button =
    document.getElementById("searchButton");

button.addEventListener("click", function() {

    const question =
        document.getElementById("question").value;

    document.getElementById("answer").textContent =
        "You asked: " + question;

});